#!/usr/bin/env python3
"""
app_update.py - portable releases and automatic updates from GitHub Releases (no GUI code here).

Used by app_update_qt.py (PySide6 / PyQt5) and app_update_tk.py (Tkinter). The product is described
in app_product.py (name, GitHub repository, contact e-mail, guides) and app_version.py (VERSION, BUILD,
stamped by the build scripts).

How it works
------------
* A release on GitHub carries, for every platform, a portable ZIP of the whole product folder and a
  small JSON manifest next to it::

      DeClim-Windows-x64.zip           DeClim-update-windows-x64.json
      DeClim-macOS-x64.zip             DeClim-update-macos-x64.json
      DeClim-macOS-arm64.zip           DeClim-update-macos-arm64.json

  The manifest holds version, build, archive name, size and SHA-256 (made by release_kit/make_release.py).
* The installed product folder (e.g. ``DeClim/``) contains ``DeClim-INSTALLED.json`` with its version,
  build, platform and the list of its own files.
* Check: GitHub API ``releases/latest`` -> manifest of this platform -> newer version/build?
* Download: the ZIP into the per-user update folder, size and SHA-256 verified.
* Prepare (program still open): unpack next to the program folder (same disk), check the identity,
  copy the user's own files that are not part of the package.
* Apply: a small helper script (PowerShell on Windows, bash on macOS) waits until every window of the
  product is closed, renames the old folder to ``<name>.previous-<id>``, moves the new one in place and
  starts the program again. The new program confirms the start; otherwise the old folder is restored.
  The newest previous version is kept for a manual rollback, older ones are removed.

Nothing is installed in the system; the product folder can live anywhere the user can write to.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import app_product as P
except Exception:  # pragma: no cover - a project without app_product.py
    P = None  # type: ignore
try:
    from app_version import BUILD, VERSION
except Exception:  # pragma: no cover
    VERSION, BUILD = "0.0.0", "0"

SCHEMA = 1
MAX_ARCHIVE = 8 * 1024 ** 3
STATUS_NAMES = {
    "waiting": "Waiting until all windows of the program are closed",
    "moving": "Replacing the program folder",
    "restarting": "Starting the new version",
    "complete": "Update completed",
    "started": "The new version started but did not confirm yet; the previous version is kept",
    "cancelled": "Update cancelled - the program was not changed",
    "error": "Update failed - the previous version was kept or restored",
}


# ---------------------------------------------------------------------------------------------------------
# product
# ---------------------------------------------------------------------------------------------------------
def product() -> str:
    return getattr(P, "PRODUCT", "App")


def display_name() -> str:
    return getattr(P, "DISPLAY_NAME", product())


def repository() -> str:
    return getattr(P, "REPOSITORY", "")


def developer() -> str:
    return getattr(P, "DEVELOPER", "the developer")


def contact_email() -> str:
    return getattr(P, "CONTACT_EMAIL", "")


def guides() -> List[Tuple[str, str]]:
    """[(label, asset file name)] attached to every release."""
    return list(getattr(P, "GUIDES", []) or [])


def releases_url() -> str:
    return f"https://github.com/{repository()}/releases"


def latest_release_url() -> str:
    return f"https://github.com/{repository()}/releases/latest"


def guide_url(asset: str) -> str:
    return f"https://github.com/{repository()}/releases/latest/download/{urllib.parse.quote(asset)}"


def version_text() -> str:
    return f"{VERSION} (build {BUILD})" if BUILD not in ("", "0") else f"{VERSION} (source)"


def version_key(version: str, build: str) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    def nums(v: str) -> Tuple[int, ...]:
        v = str(v).strip().lstrip("vV")
        if not re.fullmatch(r"\d+(?:\.\d+){0,3}", v):
            raise ValueError("Invalid version: " + v)
        p = tuple(int(x) for x in v.split("."))
        return p + (0,) * (4 - len(p))
    return nums(version), nums(build)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def platform_key() -> str:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def index_name() -> str:
    return f"{product()}-INSTALLED.json"


def manifest_name(key: str) -> str:
    return f"{product()}-update-{key}.json"


def archive_name(key: str) -> str:
    system, arch = key.split("-", 1)
    return f"{product()}-{'Windows' if system == 'windows' else 'macOS' if system == 'macos' else system}-{arch}.zip"


# ---------------------------------------------------------------------------------------------------------
# files and settings
# ---------------------------------------------------------------------------------------------------------
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, data: Any) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default


def update_home() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = base / product() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings() -> Dict[str, Any]:
    s = read_json(update_home() / "settings.json", {}) or {}
    s.setdefault("auto_check", True)
    return s


def save_settings(**values: Any) -> None:
    s = settings()
    s.update(values)
    try:
        write_json(update_home() / "settings.json", s)
    except Exception:
        pass


def log(message: str) -> None:
    try:
        with open(update_home() / "updates.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------------------
# installation
# ---------------------------------------------------------------------------------------------------------
def installed_target() -> Path:
    """The product folder of this packaged program (it holds <PRODUCT>-INSTALLED.json)."""
    if not is_frozen():
        raise RuntimeError("You run the program from the Python source: automatic installation works only "
                           "in the packaged program. Download the new version from the release page.")
    exe = Path(sys.executable).resolve()
    for parent in list(exe.parents)[:6]:
        if (parent / index_name()).is_file():
            return parent
    raise RuntimeError(f"{index_name()} was not found next to the program. Was the program folder renamed or "
                       f"copied only partly? Download the complete package from the release page.")


def installed_index(target: Optional[Path] = None) -> Dict[str, Any]:
    target = target or installed_target()
    data = read_json(target / index_name())
    if not isinstance(data, dict) or data.get("product") != product():
        raise RuntimeError("The installation index is damaged: " + str(target / index_name()))
    return data


def relaunch_path(target: Path) -> Path:
    """What to start after the update: the same program (exe or .app) that started the update."""
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
    return exe


# ---------------------------------------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------------------------------------
def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class _GithubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if not newurl.startswith("https://") or not (host == "github.com" or host.endswith(".githubusercontent.com")):
            raise ValueError("Unexpected download location: " + host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float = 30.0):
    opener = urllib.request.build_opener(_GithubRedirect(), urllib.request.HTTPSHandler(context=_ssl_context()))
    req = urllib.request.Request(url, headers={"User-Agent": f"{product()}-Updater/{VERSION}",
                                               "Accept": "application/vnd.github+json"})
    return opener.open(req, timeout=timeout)


def _get_json(url: str, limit: int = 4 * 1024 * 1024) -> Any:
    with _open(url) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Answer too large")
    return json.loads(data.decode("utf-8"))


def _official_asset(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.netloc != "github.com" or not u.path.startswith(f"/{repository()}/releases/download/"):
        raise ValueError("The update must come from the official GitHub releases of " + repository())
    return url


def check_release(cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    """Latest published release: dict(newer, version, build, tag, notes, html_url, manifest...)."""
    if not repository():
        raise RuntimeError("No GitHub repository is configured (app_product.REPOSITORY).")
    try:
        rel = _get_json(f"https://api.github.com/repos/{repository()}/releases/latest")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"newer": False, "none": True, "html_url": releases_url()}
        raise
    if cancel is not None and cancel.is_set():
        raise InterruptedError("cancelled")
    out: Dict[str, Any] = {"tag": rel.get("tag_name", ""), "name": rel.get("name", ""),
                           "notes": str(rel.get("body") or "")[:20000], "html_url": rel.get("html_url") or
                           latest_release_url(), "newer": False, "manifest": None}
    assets = {a.get("name"): a for a in rel.get("assets", [])}
    out["assets"] = sorted(assets)
    keys = [platform_key()]
    try:
        if is_frozen():
            keys = [installed_index().get("platform", keys[0])]
    except Exception:
        pass
    if keys[0] == "macos-arm64":
        keys.append("macos-x64")  # runs with Rosetta 2
    for key in keys:
        a = assets.get(manifest_name(key))
        if not a:
            continue
        man = _get_json(_official_asset(a["browser_download_url"]), 65536)
        if man.get("product") != product() or man.get("schema") != SCHEMA:
            raise ValueError("The update manifest belongs to another program")
        arc = assets.get(man.get("archive"))
        if not arc or int(arc.get("size", -1)) != int(man.get("size", -2)):
            raise ValueError("The release archive is missing or has another size than in its manifest")
        digest = arc.get("digest")
        if digest and digest != "sha256:" + man.get("sha256", ""):
            raise ValueError("GitHub and manifest checksums differ")
        man = dict(man, url=_official_asset(arc["browser_download_url"]))
        out["manifest"] = man
        out["version"], out["build"] = man["version"], man["build"]
        break
    if out["manifest"] is None:
        m = re.search(r"(\d+(?:\.\d+){0,3})", out["tag"])
        out["version"], out["build"] = (m.group(1) if m else "0"), "0"
    try:
        out["newer"] = version_key(out["version"], out["build"]) > version_key(VERSION, BUILD)
    except ValueError:
        out["newer"] = False
    save_settings(last_check=time.time())
    return out


def download_release(info: Dict[str, Any], progress: Callable[[int, int], None],
                     cancel: threading.Event) -> Path:
    """Download and verify the archive of the manifest; returns the job folder."""
    man = info["manifest"]
    work = Path(tempfile.mkdtemp(prefix="run-", dir=str(update_home())))
    part = work / "download.part"
    try:
        if shutil.disk_usage(work).free < int(man["size"]) * 3 + 300 * 1024 ** 2:
            raise OSError("Not enough free disk space for the update")
        done = 0
        with _open(_official_asset(man["url"]), timeout=60) as r, open(part, "wb") as fh:
            while True:
                if cancel.is_set():
                    raise InterruptedError("Download cancelled")
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > min(MAX_ARCHIVE, int(man["size"])):
                    raise ValueError("The download is larger than expected")
                fh.write(chunk)
                progress(done, int(man["size"]))
        if done != int(man["size"]) or sha256(part) != man["sha256"]:
            raise ValueError("The downloaded file is damaged (checksum differs); try again")
        os.replace(part, work / "download.zip")
        write_json(work / "release.json", info)
        save_settings(downloaded=str(work))
        log(f"downloaded {man['archive']} {man['version']}/{man['build']} -> {work}")
        return work
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------------------------------------
# running copies (all of them must be closed before the folder is replaced)
# ---------------------------------------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def register_running() -> None:
    """Remember this process (used to wait for every open window of the product)."""
    if not is_frozen():
        return
    try:
        target = installed_target()
    except Exception:
        return
    d = update_home() / "running"
    d.mkdir(exist_ok=True)
    for f in d.glob("*.json"):
        try:
            if not _pid_alive(int(f.stem)):
                f.unlink()
        except Exception:
            pass
    write_json(d / f"{os.getpid()}.json", {"target": str(target), "exe": sys.executable, "time": time.time()})


def running_copies(target: Path) -> List[Tuple[int, str]]:
    out = []
    for f in (update_home() / "running").glob("*.json"):
        try:
            pid = int(f.stem)
            data = read_json(f, {})
            if Path(data.get("target", "")).resolve() == target.resolve() and _pid_alive(pid):
                out.append((pid, Path(data.get("exe", "")).name))
        except Exception:
            continue
    if os.getpid() not in [p for p, _n in out]:
        out.append((os.getpid(), Path(sys.executable).name))
    return out


# ---------------------------------------------------------------------------------------------------------
# prepare and apply
# ---------------------------------------------------------------------------------------------------------
def _safe_members(zf: zipfile.ZipFile, root: str) -> None:
    for item in zf.infolist():
        name = item.filename
        p = PurePosixPath(name)
        if "\\" in name or p.is_absolute() or ".." in p.parts or not p.parts:
            raise ValueError("Unsafe path in the archive: " + name)
        if p.parts[0] not in (root, "__MACOSX"):
            raise ValueError("Unexpected folder in the archive: " + name)


def prepare_update(work: Path) -> Dict[str, Any]:
    """Unpack the verified download next to the program folder; returns the job description."""
    work = Path(work)
    info = read_json(work / "release.json")
    man = info["manifest"]
    target = installed_target()
    old = installed_index(target)
    archive = work / "download.zip"
    if archive.stat().st_size != int(man["size"]) or sha256(archive) != man["sha256"]:
        raise ValueError("The downloaded archive was changed; download it again")
    parent = target.parent
    if not os.access(parent, os.W_OK):
        raise PermissionError(f"The folder {parent} is not writable. Move the '{target.name}' folder to a place you can "
                              f"write to (e.g. Documents or your user folder) and start the update again.")
    tag = work.name.replace("run-", "")
    holder = parent / f".{target.name}-update-{tag}"
    if holder.exists():
        shutil.rmtree(holder, ignore_errors=True)
    holder.mkdir()
    with zipfile.ZipFile(archive) as zf:
        _safe_members(zf, target.name)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(holder)], check=True, capture_output=True)
        shutil.rmtree(holder / "__MACOSX", ignore_errors=True)
    else:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(holder)
    stage = holder / target.name
    new = read_json(stage / index_name())
    if not isinstance(new, dict) or new.get("version") != man["version"] or str(new.get("build")) != str(man["build"]):
        raise ValueError("The package content does not match its release manifest")
    # the user's own files in the program folder (not part of the old package) are carried over
    owned = set(old.get("files", [])) | {index_name()}
    kept, skipped = 0, []
    for f in target.rglob("*"):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(target).as_posix()
        if rel in owned or "/__pycache__/" in "/" + rel or rel.endswith(".pyc"):
            continue
        dest = stage / rel
        if dest.exists():
            skipped.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        kept += 1
    rel_launch = relaunch_path(target).relative_to(target).as_posix()
    job = {"target": str(target), "stage": str(stage), "holder": str(holder),
           "backup": str(parent / f"{target.name}.previous-{tag}"), "failed": str(parent / f"{target.name}.failed-{tag}"),
           "launch": str(target / rel_launch), "version": man["version"], "build": man["build"],
           "from_version": old.get("version"), "from_build": old.get("build"), "user_files": kept,
           "user_files_skipped": skipped[:50]}
    write_json(work / "job.json", job)
    log(f"prepared {job}")
    return job


def _ps_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _sh_str(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def _write_status(work: Path, state: str, message: str = "") -> None:
    write_json(work / "status.json", {"state": state, "message": message, "time": time.time()})


def start_apply(work: Path, pids: List[int]) -> None:
    """Start the helper script that replaces the folder once all given processes have ended."""
    work = Path(work)
    job = read_json(work / "job.json")
    _write_status(work, "waiting", "")
    for name in ("cancel", "ack"):
        try:
            (work / name).unlink()
        except FileNotFoundError:
            pass
    if sys.platform == "win32":
        script = work / "apply_update.ps1"
        pid_list = ",".join(str(int(p)) for p in pids) or "0"
        text = f"""$ErrorActionPreference = 'Continue'
$work = {_ps_str(work)}
$target = {_ps_str(job['target'])}
$stage = {_ps_str(job['stage'])}
$holder = {_ps_str(job['holder'])}
$backup = {_ps_str(job['backup'])}
$failed = {_ps_str(job['failed'])}
$launch = {_ps_str(job['launch'])}
$pids = @({pid_list})
function Write-State($state, $message) {{
  $o = @{{ state = $state; message = $message; time = [double](Get-Date -UFormat %s) }}
  $o | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $work 'status.json') -Encoding UTF8
  Add-Content -LiteralPath (Join-Path $work 'helper.log') -Value ((Get-Date -Format s) + ' ' + $state + ' ' + $message)
}}
function Start-Program($path) {{
  try {{ Start-Process -FilePath $path -WorkingDirectory (Split-Path -Parent $path) | Out-Null }} catch {{}}
}}
Write-State 'waiting' 'waiting for the program windows to close'
$deadline = (Get-Date).AddHours(12)
foreach ($p in $pids) {{
  while ((Get-Process -Id $p -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) {{
    if (Test-Path -LiteralPath (Join-Path $work 'cancel')) {{ Write-State 'cancelled' ''; exit 0 }}
    Start-Sleep -Milliseconds 500
  }}
}}
if (Test-Path -LiteralPath (Join-Path $work 'cancel')) {{ Write-State 'cancelled' ''; exit 0 }}
Write-State 'moving' ''
$ok = $false
for ($i = 0; $i -lt 180; $i++) {{
  try {{ Rename-Item -LiteralPath $target -NewName (Split-Path -Leaf $backup) -ErrorAction Stop; $ok = $true; break }}
  catch {{ Start-Sleep -Seconds 1 }}
}}
if (-not $ok) {{
  Write-State 'error' 'The program folder is still in use (another window, Explorer or antivirus). Nothing was changed.'
  Start-Program $launch
  exit 1
}}
try {{ Move-Item -LiteralPath $stage -Destination $target -ErrorAction Stop }}
catch {{
  $m = $_.Exception.Message
  try {{ Rename-Item -LiteralPath $backup -NewName (Split-Path -Leaf $target) -ErrorAction Stop }} catch {{}}
  Write-State 'error' ('Could not move the new version in place: ' + $m)
  Start-Program $launch
  exit 1
}}
Remove-Item -LiteralPath $holder -Recurse -Force -ErrorAction SilentlyContinue
Write-State 'restarting' ''
try {{ $proc = Start-Process -FilePath $launch -WorkingDirectory (Split-Path -Parent $launch) -PassThru -ErrorAction Stop }}
catch {{ $proc = $null }}
for ($i = 0; $i -lt 240; $i++) {{
  if (Test-Path -LiteralPath (Join-Path $work 'ack')) {{ Write-State 'complete' ''; exit 0 }}
  if (($proc -eq $null) -or ($proc.HasExited -and $i -gt 3)) {{
    try {{
      Rename-Item -LiteralPath $target -NewName (Split-Path -Leaf $failed) -ErrorAction Stop
      Rename-Item -LiteralPath $backup -NewName (Split-Path -Leaf $target) -ErrorAction Stop
      Write-State 'error' 'The new version did not start; the previous version was restored.'
      Start-Program $launch
    }} catch {{ Write-State 'error' ('The new version did not start and the previous one could not be restored: ' + $_.Exception.Message) }}
    exit 1
  }}
  Start-Sleep -Milliseconds 500
}}
Write-State 'started' 'no start confirmation yet'
"""
        script.write_text(text, encoding="utf-8-sig")
        flags = 0x08000000 | 0x00000200 | 0x00000008  # NO_WINDOW | NEW_PROCESS_GROUP | DETACHED
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", str(script)], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(work))
    else:
        script = work / "apply_update.sh"
        pid_list = " ".join(str(int(p)) for p in pids)
        text = f"""#!/bin/bash
work={_sh_str(work)}
target={_sh_str(job['target'])}
stage={_sh_str(job['stage'])}
holder={_sh_str(job['holder'])}
backup={_sh_str(job['backup'])}
failed={_sh_str(job['failed'])}
launch={_sh_str(job['launch'])}
state() {{
  printf '{{"state": "%s", "message": "%s", "time": %s}}\\n' "$1" "$2" "$(date +%s)" > "$work/status.json"
  echo "$(date '+%F %T') $1 $2" >> "$work/helper.log"
}}
start_program() {{ if [ -d "$1" ]; then /usr/bin/open "$1"; else "$1" >/dev/null 2>&1 & fi; }}
state waiting "waiting for the program windows to close"
for p in {pid_list}; do
  while kill -0 "$p" 2>/dev/null; do
    if [ -f "$work/cancel" ]; then state cancelled ""; exit 0; fi
    sleep 0.5
  done
done
if [ -f "$work/cancel" ]; then state cancelled ""; exit 0; fi
state moving ""
if ! mv "$target" "$backup"; then state error "The program folder could not be renamed. Nothing was changed."; start_program "$launch"; exit 1; fi
if ! mv "$stage" "$target"; then mv "$backup" "$target"; state error "The new version could not be moved in place."; start_program "$launch"; exit 1; fi
rm -rf "$holder"
/usr/bin/xattr -dr com.apple.quarantine "$target" 2>/dev/null
state restarting ""
start_program "$launch"
for i in $(seq 1 240); do
  if [ -f "$work/ack" ]; then state complete ""; exit 0; fi
  sleep 0.5
done
state started "no start confirmation yet - if the new version does not work, use 'Restore previous version'"
"""
        script.write_text(text, encoding="utf-8")
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(work))
    save_settings(pending=str(work))
    log(f"helper started for {work} (pids {pids})")


def cancel_apply(work: Path) -> None:
    try:
        (Path(work) / "cancel").write_text("cancel", encoding="utf-8")
    except Exception:
        pass


def job_status(work: Path) -> Dict[str, Any]:
    return read_json(Path(work) / "status.json", {}) or {}


# ---------------------------------------------------------------------------------------------------------
# after a restart
# ---------------------------------------------------------------------------------------------------------
def acknowledge_start() -> Optional[Dict[str, Any]]:
    """Called at start-up of the packaged program: confirm a just-applied update; returns its job."""
    register_running()
    if not is_frozen():
        return None
    try:
        target = installed_target()
    except Exception:
        return None
    home = update_home()
    found = None
    for work in sorted(home.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = job_status(work)
        job = read_json(work / "job.json", {}) or {}
        if not job or Path(job.get("target", "")).resolve() != target.resolve():
            continue
        if st.get("state") in ("restarting", "started", "moving"):
            try:
                idx = installed_index(target)
                if idx.get("version") == job.get("version") and str(idx.get("build")) == str(job.get("build")):
                    (work / "ack").write_text("started", encoding="utf-8")
                    found = dict(job, work=str(work))
                    log(f"start confirmed {job.get('version')}/{job.get('build')}")
            except Exception:
                pass
            break
        if st.get("state") == "error" and not (work / "reported").exists():
            found = dict(job, work=str(work), error=st.get("message", ""))
            (work / "reported").write_text("1", encoding="utf-8")
            break
    if found and not found.get("error"):
        threading.Thread(target=cleanup, args=(target,), daemon=True).start()
    return found


def previous_versions(target: Optional[Path] = None) -> List[Path]:
    target = target or installed_target()
    return sorted(target.parent.glob(target.name + ".previous-*"), key=lambda p: p.stat().st_mtime, reverse=True)


def cleanup(target: Path, keep: int = 1) -> None:
    """Keep the newest previous version only; remove failed copies and old downloads."""
    time.sleep(20)
    for p in previous_versions(target)[keep:] + list(target.parent.glob(target.name + ".failed-*")):
        shutil.rmtree(p, ignore_errors=True)
        log(f"removed {p}")
    home = update_home()
    runs = sorted(home.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for k, work in enumerate(runs):
        if job_status(work).get("state") in ("complete", "started", "cancelled", "error") or k >= 3:
            try:
                (work / "download.zip").unlink()
            except FileNotFoundError:
                pass
        if k >= 10:
            shutil.rmtree(work, ignore_errors=True)


def start_rollback(pids: List[int]) -> Path:
    """Swap the program folder with the newest previous version (helper script, after closing)."""
    target = installed_target()
    prev = previous_versions(target)
    if not prev:
        raise RuntimeError("No previous version is kept next to the program folder.")
    work = Path(tempfile.mkdtemp(prefix="run-", dir=str(update_home())))
    idx = read_json(prev[0] / index_name(), {}) or {}
    tag = work.name.replace("run-", "")
    holder = target.parent / f".{target.name}-update-{tag}"
    holder.mkdir()
    stage = holder / target.name
    os.replace(prev[0], stage)
    job = {"target": str(target), "stage": str(stage), "holder": str(holder),
           "backup": str(target.parent / f"{target.name}.previous-{tag}"),
           "failed": str(target.parent / f"{target.name}.failed-{tag}"),
           "launch": str(target / relaunch_path(target).relative_to(target).as_posix()),
           "version": idx.get("version", "?"), "build": idx.get("build", "?"), "rollback": True}
    write_json(work / "job.json", job)
    start_apply(work, pids)
    return work


# ---------------------------------------------------------------------------------------------------------
# contact
# ---------------------------------------------------------------------------------------------------------
def system_summary() -> str:
    return (f"{display_name()} {version_text()}\n"
            f"Program: {Path(sys.executable).name if is_frozen() else 'Python source'}\n"
            f"System: {platform.system()} {platform.release()} ({platform.machine()}), Python {platform.python_version()}")


def mailto_url(kind: str, message: str, name: str = "", email: str = "", include_system: bool = True) -> str:
    subject = f"[{product()}] {kind}"
    body = message.strip()
    if name or email:
        body += f"\n\nFrom: {name} {('<' + email + '>') if email else ''}".rstrip()
    if include_system:
        body += "\n\n---\n" + system_summary()
    return ("mailto:" + contact_email() + "?" +
            urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote))


def issue_url(kind: str, message: str, include_system: bool = True) -> str:
    body = message.strip() + (("\n\n---\n" + system_summary()) if include_system else "")
    title = f"{kind}: " + (message.strip().splitlines() or [""])[0][:70]
    return (f"https://github.com/{repository()}/issues/new?" +
            urllib.parse.urlencode({"title": title, "body": body[:6000]}, quote_via=urllib.parse.quote))


def local_guides() -> List[Path]:
    """Guides shipped inside the product folder (docs/)."""
    try:
        base = installed_target()
    except Exception:
        base = Path(__file__).resolve().parent.parent
    d = base / "docs"
    return sorted(p for p in d.glob("*") if p.suffix.lower() in (".pdf", ".docx", ".html", ".md")) if d.is_dir() else []


def open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def due_for_auto_check() -> bool:
    s = settings()
    if not s.get("auto_check", True):
        return False
    return time.time() - float(s.get("last_check", 0) or 0) > 20 * 3600


def help_text() -> str:
    return (f"{display_name()} checks the official GitHub releases of {repository()} once a day (can be switched "
            f"off). A new version is downloaded only after you agree; its checksum is verified. The update is "
            f"installed when all windows of the program are closed: the program folder is replaced by the new "
            f"one, your own files in it are kept, and the previous version stays next to it as "
            f"'<folder>.previous-...' so that you can go back with 'Restore previous version'. Settings and "
            f"projects outside the program folder are not touched. Nothing is installed in the system.\n\n"
            f"The program folder must be in a place where you can write (e.g. Documents, Desktop or your user "
            f"folder). When the program runs from the Python source, the update is downloaded from the "
            f"release page instead.\n\nUpdate log: {update_home()}")
