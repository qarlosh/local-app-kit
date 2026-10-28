import json
import plistlib
import zipfile

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from local_app_kit import LocalApp, launchd

BASE = "http://127.0.0.1:7701"


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_APP_DATA_DIR", str(tmp_path / "data"))
    for key in ("PORT", "PUBLIC_HOST", "ROOT_PATH"):
        monkeypatch.delenv(f"MY_APP_{key}", raising=False)


def make(llm_check=None, **kwargs):
    kit = LocalApp("my-app", version="1.2.0", port=7701, **kwargs)
    app = FastAPI()

    @app.get("/")
    def home(request: Request):
        return {"prefix": request.scope["root_path"], "health": str(request.url_for("health"))}

    kit.setup(app, llm_check=llm_check)
    return kit, app


def test_health_without_llm(tmp_path):
    _, app = make()
    response = TestClient(app, base_url=BASE).get("/api/health")
    assert response.json() == {
        "status": "ok",
        "app": "my-app",
        "version": "1.2.0",
        "data_dir": str(tmp_path / "data"),
        "llm": None,
    }
    assert response.headers["content-security-policy"] == "default-src 'self'"


def test_health_reports_the_llm():
    _, app = make(llm_check=lambda: True)
    assert TestClient(app, base_url=BASE).get("/api/health").json()["llm"] is True

    def down():
        raise RuntimeError("claude not logged in")

    _, app = make(llm_check=down)
    answer = TestClient(app, base_url=BASE).get("/api/health").json()
    assert answer["llm"] is False and "not logged in" in answer["llm_error"]


def test_setup_protects_the_app():
    _, app = make()
    client = TestClient(app, base_url=BASE)
    assert client.get("/", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_public_host_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("MY_APP_PUBLIC_HOST", "my-app.ngrok.app")
    _, app = make(exposure="public")
    client = TestClient(app, base_url="https://my-app.ngrok.app")
    assert client.get("/api/health", headers={"Origin": "https://my-app.ngrok.app"}).status_code == 200


def test_public_without_host_fails_at_setup():
    with pytest.raises(ValueError):
        make(exposure="public")


def test_works_under_a_path_prefix(monkeypatch):
    monkeypatch.setenv("MY_APP_ROOT_PATH", "/my-app/")
    kit, app = make()
    assert kit.root_path == "/my-app"
    # The proxy strips the prefix; the server is told about it, as uvicorn does with root_path.
    client = TestClient(app, base_url=BASE, root_path=kit.root_path)
    assert client.get("/").json() == {"prefix": "/my-app", "health": f"{BASE}/my-app/api/health"}
    assert client.get("/api/health").status_code == 200


def test_serve_migrates_then_runs_uvicorn(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_APP_PORT", "7799")
    monkeypatch.setenv("MY_APP_ROOT_PATH", "/my-app")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "items.json").write_text("[]")
    migrated = []
    kit, app = make(format_version=2, migrations={1: migrated.append})
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("local_app_kit.logs.setup_logging", lambda path: None)
    kit.main(app, [])
    assert migrated == [tmp_path / "data"]
    assert calls == [{"host": "127.0.0.1", "port": 7799, "root_path": "/my-app", "log_config": None}]


def test_install_runs_this_project_through_uv(monkeypatch, tmp_path, capsys):
    project = tmp_path / "repo"
    (project / "src").mkdir(parents=True)
    (project / "pyproject.toml").write_text("")
    monkeypatch.chdir(project / "src")
    monkeypatch.setattr("sys.argv", [str(project / ".venv" / "bin" / "my-app"), "install"])
    monkeypatch.setattr("shutil.which", lambda name: "/bin/uv")
    monkeypatch.setenv("MY_APP_PORT", "7702")
    monkeypatch.setenv("PATH", f"{project}/.venv/bin:/usr/bin")
    # "print" fails: the old service is already gone, so install does not wait for it.
    monkeypatch.setattr(launchd, "_launchctl", lambda *a: launchd.subprocess.CompletedProcess(a, a[0] == "print", "", ""))
    kit, app = make()
    kit.main(app, ["install"])
    plist = plistlib.loads(launchd.plist_path("local.my-app").read_bytes())
    assert plist["ProgramArguments"] == ["/bin/uv", "run", "--project", str(project), "my-app", "serve"]
    assert plist["EnvironmentVariables"] == {
        "MY_APP_PORT": "7702",
        "MY_APP_DATA_DIR": str(tmp_path / "data"),
        "PATH": "/usr/bin",
    }
    assert "http://127.0.0.1:7702/" in capsys.readouterr().out


def test_restart_when_not_installed_exits_with_the_reason(monkeypatch):
    monkeypatch.setattr(launchd, "_launchctl", lambda *a: launchd.subprocess.CompletedProcess(a, 113, "", "no such service"))
    kit, app = make()
    with pytest.raises(SystemExit, match="no such service"):
        kit.main(app, ["restart"])


def test_export_command(tmp_path):
    kit, app = make()
    (kit.data_dir / "items.json").write_text(json.dumps([1]))
    kit.main(app, ["export", str(tmp_path / "out.zip")])
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        assert zf.namelist() == ["items.json"]


def test_install_adds_the_apps_launchd_keys(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/uv")
    monkeypatch.setattr(launchd, "_launchctl", lambda *a: launchd.subprocess.CompletedProcess(a, a[0] == "print", "", ""))
    kit, app = make(launchd_extra={"AbandonProcessGroup": True})
    kit.main(app, ["install"])
    assert plistlib.loads(launchd.plist_path("local.my-app").read_bytes())["AbandonProcessGroup"] is True


def test_own_commands_get_their_arguments():
    kit, app = make()
    calls = []
    kit.main(app, ["port", "new-app", "--json"], commands={"port": calls.append})
    assert calls == [["new-app", "--json"]]


def test_own_commands_cannot_replace_the_built_in_ones():
    kit, app = make()
    with pytest.raises(ValueError, match="install"):
        kit.main(app, ["install"], commands={"install": print})


def test_unknown_command_and_stray_arguments_are_errors():
    kit, app = make()
    with pytest.raises(SystemExit):
        kit.main(app, ["port"])
    with pytest.raises(SystemExit):
        kit.main(app, ["restart", "now"])
