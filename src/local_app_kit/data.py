"""The app's data folder: where it lives, safe writes, format migrations and export.

The data lives outside the repo, so deleting or recloning the repo never loses it and it cannot be
committed by mistake. Files are written with mode 0600 and the folder with 0700, since app data is
often personal.
"""

import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

FORMAT_FILE = "format.json"
BACKUPS = "backups"
LOGS = "logs"
# Not data: left out of backups and exports.
NOT_DATA = frozenset({BACKUPS, LOGS})

Migration = Callable[[Path], None]


def env_prefix(app_name: str) -> str:
    """`my-app` -> `MY_APP`, the prefix of the app's environment variables."""
    return app_name.upper().replace("-", "_").replace(".", "_")


def data_dir(app_name: str) -> Path:
    """`~/.local/share/<app_name>`, or the path in `<PREFIX>_DATA_DIR` (for tests and trials)."""
    custom = os.environ.get(f"{env_prefix(app_name)}_DATA_DIR")
    path = Path(custom).expanduser() if custom else Path.home() / ".local" / "share" / app_name
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    """Write to a temporary file next to `path`, then rename it over `path`.

    A crash or a full disk leaves either the old file or the new one, never half of each.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode() if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode)


class DataFormatError(RuntimeError):
    """The data cannot be brought to the format this code expects."""


def _has_data(folder: Path) -> bool:
    return any(p.name not in NOT_DATA for p in folder.iterdir())


def format_version(folder: Path) -> int | None:
    """The data's format version; None for an empty folder.

    Data written before the app used versions has no format file and counts as version 1.
    """
    file = folder / FORMAT_FILE
    if file.exists():
        return int(json.loads(file.read_text())["format_version"])
    return 1 if _has_data(folder) else None


def backup(folder: Path, label: str) -> Path:
    """Copy the data (not older backups or logs) to `backups/<label>-<time>/`."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / BACKUPS / f"{label}-{stamp}"
    shutil.copytree(folder, target, ignore=lambda d, names: NOT_DATA if Path(d) == folder else ())
    return target


def migrate(folder: Path, version: int, migrations: Mapping[int, Migration]) -> Path | None:
    """Bring the data in `folder` to format `version`, backing it up first.

    `migrations[n]` turns format `n` into `n + 1`, working on the files in `folder`. After each
    step the new version is saved, so a failed step resumes from where it stopped. Returns the
    backup folder, or None when nothing had to change.
    """
    current = format_version(folder)
    if current is None:
        atomic_write_json(folder / FORMAT_FILE, {"format_version": version})
        return None
    if current > version:
        raise DataFormatError(f"data is format {current}, newer than this code's {version}; update the app")
    if current == version:
        return None
    missing = [n for n in range(current, version) if n not in migrations]
    if missing:
        raise DataFormatError(f"no migration from format {missing[0]} to {missing[0] + 1}")
    saved = backup(folder, f"format-{current}")
    for n in range(current, version):
        migrations[n](folder)
        atomic_write_json(folder / FORMAT_FILE, {"format_version": n + 1})
    return saved


def export_data(folder: Path, target: Path) -> Path:
    """Zip every data file (not backups or logs) into `target`."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if relative.parts[0] in NOT_DATA or not path.is_file() or path.resolve() == target.resolve():
                continue
            zf.write(path, relative.as_posix())
    os.chmod(target, 0o600)
    return target
