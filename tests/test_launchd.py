import plistlib
import subprocess

import pytest

from local_app_kit import launchd


class FakeLaunchctl:
    """Records launchctl calls and keeps the set of loaded labels."""

    def __init__(self):
        self.calls = []
        self.loaded = set()
        self.fail = set()

    def __call__(self, *args):
        self.calls.append(args)
        verb, target = args[0], args[-1]
        label = target.rsplit("/", 1)[-1].removesuffix(".plist")
        code = 0
        if verb in self.fail:
            code = 1
        elif verb == "bootstrap":
            self.loaded.add(label)
        elif verb == "bootout":
            self.loaded.discard(label)
        elif verb in ("print", "kickstart"):
            code = 0 if label in self.loaded else 113
        return subprocess.CompletedProcess(args, code, "", "boom" if code else "")


@pytest.fixture
def launchctl(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_launchctl", fake)
    return fake


def install(tmp_path, **kwargs):
    return launchd.install(
        "local.my-app",
        ["/usr/bin/uv", "run", "my-app", "serve"],
        working_dir=tmp_path / "repo",
        env={"PATH": "/usr/bin", "MY_APP_PORT": "7701"},
        log_file=tmp_path / "data" / "logs" / "launchd.log",
        **kwargs,
    )


def test_install_writes_the_agent_and_loads_it(launchctl, tmp_path):
    path = install(tmp_path)
    assert path == tmp_path / "Library" / "LaunchAgents" / "local.my-app.plist"
    plist = plistlib.loads(path.read_bytes())
    assert plist["ProgramArguments"] == ["/usr/bin/uv", "run", "my-app", "serve"]
    assert plist["EnvironmentVariables"]["MY_APP_PORT"] == "7701"
    assert plist["KeepAlive"] and plist["RunAtLoad"]
    assert "local.my-app" in launchctl.loaded


def test_extra_keys_go_into_the_agent(launchctl, tmp_path):
    path = install(tmp_path, extra={"AbandonProcessGroup": True})
    assert plistlib.loads(path.read_bytes())["AbandonProcessGroup"] is True


def test_extra_keys_cannot_replace_the_core_ones(launchctl, tmp_path):
    with pytest.raises(ValueError, match="KeepAlive"):
        install(tmp_path, extra={"KeepAlive": False})
    assert launchctl.calls == []


def test_install_twice_replaces(launchctl, tmp_path):
    install(tmp_path)
    install(tmp_path)
    assert launchctl.loaded == {"local.my-app"}
    assert [c[0] for c in launchctl.calls].count("bootout") == 2


def test_uninstall_undoes_install(launchctl, tmp_path):
    path = install(tmp_path)
    launchd.uninstall("local.my-app")
    assert not path.exists()
    assert launchctl.loaded == set()
    launchd.uninstall("local.my-app")  # again: nothing left, no error


def test_restart(launchctl, tmp_path):
    with pytest.raises(launchd.LaunchdError):
        launchd.restart("local.my-app")
    install(tmp_path)
    launchd.restart("local.my-app")
    assert launchctl.calls[-1][:2] == ("kickstart", "-k")


def test_rejected_bootstrap_is_an_error(launchctl, tmp_path):
    launchctl.fail.add("bootstrap")
    with pytest.raises(launchd.LaunchdError, match="boom"):
        install(tmp_path)


def test_clean_path(tmp_path, monkeypatch):
    monkeypatch.setenv("FNM_DIR", str(tmp_path / "fnm"))
    (tmp_path / "fnm" / "aliases" / "default" / "bin").mkdir(parents=True)
    path = "/repo/.venv/bin:/run/fnm_multishells/123_456/bin:/usr/bin:/usr/bin:/opt/bin"
    assert launchd.clean_path(path) == f"{tmp_path}/fnm/aliases/default/bin:/usr/bin:/opt/bin"
