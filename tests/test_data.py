import json
import stat
import zipfile

import pytest

from local_app_kit.data import (
    DataFormatError,
    atomic_write,
    atomic_write_json,
    data_dir,
    env_prefix,
    export_data,
    format_version,
    migrate,
)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_env_prefix():
    assert env_prefix("my-app") == "MY_APP"


def test_data_dir_defaults_to_local_share(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MY_APP_DATA_DIR", raising=False)
    path = data_dir("my-app")
    assert path == tmp_path / ".local" / "share" / "my-app"
    assert path.is_dir() and mode(path) == 0o700


def test_data_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_APP_DATA_DIR", str(tmp_path / "elsewhere"))
    assert data_dir("my-app") == tmp_path / "elsewhere"


def test_atomic_write_replaces_and_is_private(tmp_path):
    path = tmp_path / "sub" / "items.json"
    atomic_write_json(path, {"a": "ñ"})
    atomic_write_json(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 2}
    assert mode(path) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["items.json"]


def test_atomic_write_keeps_the_old_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    atomic_write(path, "old")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("local_app_kit.data.os.replace", broken_replace)
    with pytest.raises(OSError):
        atomic_write(path, "new")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]


def test_empty_folder_starts_at_current_format(tmp_path):
    assert migrate(tmp_path, 3, {}) is None
    assert format_version(tmp_path) == 3


def test_data_without_format_file_is_version_1(tmp_path):
    (tmp_path / "items.json").write_text("[]")
    assert format_version(tmp_path) == 1


def test_migrate_backs_up_then_runs_each_step(tmp_path):
    (tmp_path / "items.json").write_text('["a"]')
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log").write_text("log")

    def v1_to_v2(folder):
        items = json.loads((folder / "items.json").read_text())
        atomic_write_json(folder / "items.json", [{"name": i} for i in items])

    def v2_to_v3(folder):
        (folder / "items.json").rename(folder / "things.json")

    saved = migrate(tmp_path, 3, {1: v1_to_v2, 2: v2_to_v3})
    assert json.loads((tmp_path / "things.json").read_text()) == [{"name": "a"}]
    assert format_version(tmp_path) == 3
    assert saved is not None and saved.parent == tmp_path / "backups"
    assert sorted(p.name for p in saved.iterdir()) == ["items.json"]
    assert (saved / "items.json").read_text() == '["a"]'
    assert migrate(tmp_path, 3, {}) is None


def test_failed_step_resumes_from_where_it_stopped(tmp_path):
    (tmp_path / "items.json").write_text("[]")
    calls = []

    def ok(folder):
        calls.append("ok")

    def broken(folder):
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        migrate(tmp_path, 3, {1: ok, 2: broken})
    assert format_version(tmp_path) == 2
    migrate(tmp_path, 3, {1: ok, 2: ok})
    assert calls == ["ok", "ok"]


def test_missing_migration_and_newer_data_are_errors(tmp_path):
    atomic_write_json(tmp_path / "format.json", {"format_version": 2})
    with pytest.raises(DataFormatError, match="newer"):
        migrate(tmp_path, 1, {})
    with pytest.raises(DataFormatError, match="from format 2 to 3"):
        migrate(tmp_path, 3, {})
    assert not (tmp_path / "backups").exists()


def test_export_has_all_data_but_no_backups_or_logs(tmp_path):
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "items.json").write_text("[]")
    (data / "sub" / "note.md").write_text("hi")
    (data / "logs").mkdir()
    (data / "logs" / "app.log").write_text("log")
    (data / "backups" / "old").mkdir(parents=True)
    target = export_data(data, tmp_path / "out" / "export.zip")
    with zipfile.ZipFile(target) as zf:
        assert sorted(zf.namelist()) == ["items.json", "sub/note.md"]
    assert mode(target) == 0o600
