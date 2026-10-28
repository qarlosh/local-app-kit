# local-app-kit

The plumbing that small FastAPI apps running on your own Mac all need, in one place: a guard
against requests from other websites, a health endpoint, a data folder outside the repo, safe
writes, data format migrations, export, rotating logs, and a launchd service you can install and
remove with one command each.

- **Request guard:** a middleware that checks `Host`, `Origin` and `Sec-Fetch-Site`, so a web page
  open in your browser cannot use the app (CSRF) or read it through a domain of its own that
  points at 127.0.0.1 (DNS rebinding). curl and scripts, which send no `Origin`, pass. Every
  response gets `Content-Security-Policy: default-src 'self'`.
- **Exposure levels:** `local` answers only to `127.0.0.1`, `localhost` and `::1`; `public` also
  to the domain of a tunnel (ngrok) that puts a login in front of the app.
- **`GET /api/health`:** whether it is up, its version, where its data is and, if the app gives
  a check, whether its LLM responds.
- **Path prefix:** works behind a reverse proxy that serves it under `/my-app`.
- **Data folder:** `~/.local/share/<app>`, created `0700`; files written `0600`.
- **Atomic writes:** a temporary file and a rename, so a crash never leaves half a file.
- **Data format versions:** migrations run at startup after a backup copy; `export` zips it all.
- **Rotating logs:** 1 MB per file, 5 old files.
- **launchd:** `install`, `uninstall` and `restart` subcommands for the app's own script.

macOS only for launchd; the rest works anywhere.

## Install

From git, pinned to a version tag (there is no PyPI release):

```bash
uv add git+https://github.com/qarlosh/local-app-kit@v0.1.1
```

Requires Python 3.12+. It brings FastAPI and uvicorn.

## Example

`hello.py`:

```python
from fastapi import FastAPI

from local_app_kit import LocalApp, atomic_write_json

kit = LocalApp("hello", version="0.1.0", port=8765)
app = kit.setup(FastAPI(title="Hello"))


@app.get("/api/greeting")
def greeting() -> dict[str, str]:
    atomic_write_json(kit.data_dir / "last.json", {"said": "hello"})
    return {"text": "hello"}


if __name__ == "__main__":
    kit.main(app)
```

```bash
uv run hello.py
curl http://127.0.0.1:8765/api/health
curl -H "Origin: https://evil.example" http://127.0.0.1:8765/api/greeting   # 403
uv run hello.py install     # always on, at login too
uv run hello.py uninstall
```

In a packaged app, point the script at a function that calls `kit.main(app)`:

```toml
[project.scripts]
hello = "hello.app:main"
```

## `LocalApp`

```python
LocalApp(name, version, port, exposure="local", format_version=1, migrations={}, launchd_extra={})
```

The name sets everything else: environment variables start with `<PREFIX>_` (`my-app` ->
`MY_APP_`), the data lives in `~/.local/share/<name>`, logs in its `logs/` folder, and the launchd
label is `local.<name>`.

| Variable | Effect |
|---|---|
| `<PREFIX>_PORT` | Port instead of `port`. It always listens on 127.0.0.1 only. |
| `<PREFIX>_DATA_DIR` | Data folder instead of `~/.local/share/<name>`; for tests and trials. |
| `<PREFIX>_PUBLIC_HOST` | The tunnel's domain; required with `exposure="public"`. |
| `<PREFIX>_ROOT_PATH` | Path prefix the proxy serves the app under, such as `/my-app`. |

- `kit.setup(app, llm_check=None)` adds the middleware and `/api/health` to a FastAPI app and
  returns it. `llm_check` is a function called on each health request; a falsy return or an
  exception reports `"llm": false`, with the exception in `llm_error`.
  The CSP blocks anything the page loads from another site, FastAPI's `/docs` included (it
  loads Swagger UI from a CDN); `/openapi.json` still works. Serve scripts, styles and fonts
  from the app itself.
- `kit.main(app, commands={})` is the app's command line:
  - `serve` (the default): logs to the rotating file, migrates the data, then serves.
  - `install`: a LaunchAgent that runs `uv run --project <project> <script> serve` at login and
    restarts it if it stops. Run it from the project; it copies the terminal's `PATH` (so the
    service finds `claude`, `git`…) and every `<PREFIX>_*` variable that is set.
  - `uninstall`: removes exactly what `install` added. The data stays.
  - `restart`: after changing the code.
  - `export [file.zip]`: every data file, without backups or logs.
  - `commands` adds the app's own: `{"port": reserve_port}` runs `reserve_port(["new-app"])` for
    `hello port new-app`. They cannot replace the ones above.
- `launchd_extra` adds keys to the launchd agent, such as `{"AbandonProcessGroup": True}` for an
  app that starts long-running commands, so a restart does not kill them. It cannot replace the
  keys `install` sets.
- `kit.data_dir`, `kit.log_file`, `kit.prepare()` (logging and migration, for apps that start
  uvicorn their own way).

### Behind a proxy

A reverse proxy (Caddy, say) that serves the app under `/my-app` must strip the prefix, and the
app must know it to build its links: set `<PREFIX>_ROOT_PATH=/my-app`. Use relative links in the
pages (`api/greeting`, not `/api/greeting`), or `request.url_for()`. The proxy must pass the
original `Host` (Caddy does by default), or the guard rejects it.

### Data formats and migrations

`format.json` in the data folder holds the data's format version. Data from before the app had one
counts as format 1, and an empty folder starts at the current one. When the code expects a newer
format, `serve` copies the data to `backups/format-<old>-<time>/` and runs each step in turn:

```python
def items_to_objects(folder: Path) -> None:  # format 1 -> 2
    items = json.loads((folder / "items.json").read_text())
    atomic_write_json(folder / "items.json", [{"name": i} for i in items])

kit = LocalApp("hello", version="0.2.0", port=8765, format_version=2, migrations={1: items_to_objects})
```

The version is saved after each step, so a step that fails resumes there next time. Data newer
than the code, or a missing step, stops the app with an error instead of guessing.

### The pieces on their own

`SecurityMiddleware` with `allowed_hosts(exposure, public_host)`, `data_dir(name)`,
`atomic_write(path, str | bytes)`, `atomic_write_json(path, value)`, `migrate(folder, version,
migrations)`, `export_data(folder, target)` and `setup_logging(log_file)` work without
`LocalApp`. The launchd functions are in `local_app_kit.launchd`.

## Not done yet

- **`lan` exposure** (listening on the home network for devices with no other way in) needs a
  login inside the app. Not built: only `local` and `public`, which gets its login from the
  tunnel.
- Masking secrets in logs.

## Development

```bash
uv run pytest
```
