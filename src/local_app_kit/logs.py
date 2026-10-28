"""Logs that rotate, so a service that runs for months does not fill the disk."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 1_000_000
BACKUP_COUNT = 5
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(log_file: Path, level: int = logging.INFO) -> None:
    """Send the root logger (uvicorn's included) to `log_file`, rotated at 1 MB with 5 old files.

    Also to stderr when it is a terminal. Under launchd it is not, and launchd's own log file would
    never rotate. Calling it again replaces the handlers instead of adding more.
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    ]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, format=FORMAT, handlers=handlers, force=True)
