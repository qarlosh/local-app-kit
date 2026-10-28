"""Keep an app always running with launchd (macOS), and take it out again.

`install` writes a LaunchAgent that starts the app at login and restarts it if it dies;
`uninstall` removes exactly that; `restart` applies code changes without reinstalling.
"""

import os
import plistlib
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class LaunchdError(RuntimeError):
    pass


def plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def clean_path(path: str) -> str:
    """The terminal's PATH, fit for a service that outlives the terminal.

    Drops virtual environments (the one `uv run` puts first) and replaces fnm's per-shell folders,
    which vanish when the shell closes, with its default alias. Keeps the rest, so the service
    finds the same tools (`claude`, `git`) as the terminal that installed it.
    """
    fnm_default = Path(os.environ.get("FNM_DIR", Path.home() / ".local" / "share" / "fnm")) / "aliases" / "default" / "bin"
    kept: list[str] = []
    for folder in path.split(":"):
        if "fnm_multishells" in folder:
            folder = str(fnm_default) if fnm_default.is_dir() else ""
        if "/.venv/" in folder + "/":
            continue
        if folder and folder not in kept:
            kept.append(folder)
    return ":".join(kept)


def _remove(label: str) -> None:
    """Take the service out of launchd and wait until it is gone (bootout does not wait)."""
    _launchctl("bootout", f"{_domain()}/{label}")
    for _ in range(50):
        if _launchctl("print", f"{_domain()}/{label}").returncode != 0:
            return
        time.sleep(0.1)


def install(
    label: str,
    command: Sequence[str],
    *,
    working_dir: Path,
    env: Mapping[str, str],
    log_file: Path,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """Write the LaunchAgent and load it. Replaces an earlier install with the same label.

    `log_file` gets what the process prints outside the app's own logging (crashes at startup).
    `extra` adds launchd keys, such as `{"AbandonProcessGroup": True}` so a restart does not kill
    the processes the app started; it cannot replace the keys set here.
    """
    log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    plist = {
        "Label": label,
        "ProgramArguments": list(command),
        "WorkingDirectory": str(working_dir),
        "EnvironmentVariables": dict(env),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
    }
    clash = sorted(set(extra or {}) & set(plist))
    if clash:
        raise ValueError(f"extra cannot replace {', '.join(clash)}")
    plist |= dict(extra or {})
    path = plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    _remove(label)
    path.write_bytes(plistlib.dumps(plist))
    result = _launchctl("bootstrap", _domain(), str(path))
    if result.returncode != 0:
        raise LaunchdError(f"launchctl did not accept it: {result.stderr.strip()}")
    return path


def uninstall(label: str) -> None:
    _remove(label)
    plist_path(label).unlink(missing_ok=True)


def restart(label: str) -> None:
    result = _launchctl("kickstart", "-k", f"{_domain()}/{label}")
    if result.returncode != 0:
        raise LaunchdError(f"not installed, or it could not restart: {result.stderr.strip()}")
