import logging
from logging.handlers import RotatingFileHandler

import pytest

from local_app_kit.logs import BACKUP_COUNT, MAX_BYTES, setup_logging


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for h in root.handlers:
        h.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_logs_go_to_a_rotating_file(tmp_path):
    log_file = tmp_path / "logs" / "my-app.log"
    setup_logging(log_file)
    setup_logging(log_file)
    logging.getLogger("uvicorn.error").info("started")
    files = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert len(files) == 1
    assert (files[0].maxBytes, files[0].backupCount) == (MAX_BYTES, BACKUP_COUNT)
    files[0].flush()
    assert "INFO uvicorn.error: started" in log_file.read_text()


def test_file_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr("local_app_kit.logs.MAX_BYTES", 200)
    log_file = tmp_path / "my-app.log"
    setup_logging(log_file)
    for i in range(20):
        logging.getLogger("app").info("line %d %s", i, "x" * 20)
    assert (tmp_path / "my-app.log.1").exists()
    assert log_file.stat().st_size <= 200
