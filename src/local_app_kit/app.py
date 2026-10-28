"""`LocalApp`: one object per app that ties the pieces to the app's name.

The name gives the environment prefix (`my-app` -> `MY_APP_*`), the data folder, the log file and
the launchd label, so the app only states what differs: its version, port and exposure.
"""

import argparse
import logging
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from . import data, launchd, logs
from .security import SecurityMiddleware, allowed_hosts

COMMANDS = ("serve", "install", "uninstall", "restart", "export")


@dataclass
class LocalApp:
    """Settings of one app. Environment variables `<PREFIX>_PORT`, `_PUBLIC_HOST`, `_ROOT_PATH`
    and `_DATA_DIR` override the code's values without touching it.

    - `port`: where it listens, always on 127.0.0.1.
    - `exposure`: `local` (only this Mac) or `public` (also through a tunnel at `<PREFIX>_PUBLIC_HOST`).
    - `format_version` and `migrations`: see `data.migrate`; they run before serving.
    - `launchd_extra`: more keys for the launchd agent; see `launchd.install`.
    """

    name: str
    version: str
    port: int
    exposure: str = "local"
    format_version: int = 1
    migrations: Mapping[int, data.Migration] = field(default_factory=dict)
    launchd_extra: Mapping[str, Any] = field(default_factory=dict)

    def env(self, key: str, default: str | None = None) -> str | None:
        return os.environ.get(f"{data.env_prefix(self.name)}_{key}", default)

    @property
    def data_dir(self) -> Path:
        return data.data_dir(self.name)

    @property
    def log_file(self) -> Path:
        return self.data_dir / data.LOGS / f"{self.name}.log"

    @property
    def label(self) -> str:
        return f"local.{self.name}"

    @property
    def listen_port(self) -> int:
        return int(self.env("PORT") or self.port)

    @property
    def root_path(self) -> str:
        """Path prefix when a proxy serves the app under one (`/my-app`) and strips it."""
        return (self.env("ROOT_PATH") or "").rstrip("/")

    def setup(self, app: FastAPI, llm_check: Callable[[], Any] | None = None) -> FastAPI:
        """Add the security middleware and `GET /api/health` to `app`.

        `llm_check`, if given, is called on each health request: it answers whether the LLM the
        app uses responds. A falsy return or an exception means it does not.
        """
        app.add_middleware(SecurityMiddleware, hosts=allowed_hosts(self.exposure, self.env("PUBLIC_HOST")))

        @app.get("/api/health")
        def health() -> dict[str, Any]:
            answer: dict[str, Any] = {
                "status": "ok",
                "app": self.name,
                "version": self.version,
                "data_dir": str(self.data_dir),
                "llm": None,
            }
            if llm_check is not None:
                try:
                    answer["llm"] = bool(llm_check())
                except Exception as e:
                    answer["llm"], answer["llm_error"] = False, f"{type(e).__name__}: {e}"
            return answer

        return app

    def prepare(self) -> None:
        """What runs before serving: logging, then data migration. `serve` calls it."""
        logs.setup_logging(self.log_file)
        saved = data.migrate(self.data_dir, self.format_version, self.migrations)
        if saved:
            logging.getLogger(__name__).info("data migrated to format %d; backup in %s", self.format_version, saved)

    def serve(self, app: FastAPI) -> None:
        import uvicorn

        self.prepare()
        # log_config=None: uvicorn's loggers go to the root logger, so to the rotating file.
        uvicorn.run(app, host="127.0.0.1", port=self.listen_port, root_path=self.root_path, log_config=None)

    def install(self) -> Path:
        """Install as a launchd service that runs `uv run <this script> serve` in this project."""
        uv = shutil.which("uv")
        if not uv:
            raise SystemExit("uv is not on the PATH.")
        project = _find_project(Path.cwd())
        prefix = data.env_prefix(self.name) + "_"
        env = {k: v for k, v in os.environ.items() if k.startswith(prefix)}
        env["PATH"] = launchd.clean_path(os.environ.get("PATH", ""))
        script = Path(sys.argv[0]).name
        return launchd.install(
            self.label,
            [uv, "run", "--project", str(project), script, "serve"],
            working_dir=project,
            env=env,
            log_file=self.data_dir / data.LOGS / "launchd.log",
            extra=self.launchd_extra,
        )

    def main(
        self,
        app: FastAPI,
        argv: Sequence[str] | None = None,
        commands: Mapping[str, Callable[[list[str]], None]] | None = None,
    ) -> None:
        """Command line for the app's script: `serve` (default), `install`, `uninstall`,
        `restart` and `export [file]`.

        `commands` adds the app's own subcommands; each gets the arguments that follow its name.
        """
        own = dict(commands or {})
        clash = sorted(set(own) & set(COMMANDS))
        if clash:
            raise ValueError(f"commands cannot replace {', '.join(clash)}")
        parser = argparse.ArgumentParser(prog=self.name)
        parser.add_argument("command", nargs="?", default="serve", choices=(*COMMANDS, *own))
        parser.add_argument("rest", nargs=argparse.REMAINDER, help="export: the zip to write")
        args = parser.parse_args(argv)
        if args.command in own:
            own[args.command](args.rest)
            return
        if len(args.rest) > (1 if args.command == "export" else 0):
            parser.error(f"unexpected arguments for {args.command}: {' '.join(args.rest)}")
        url = f"http://127.0.0.1:{self.listen_port}{self.root_path}/"
        try:
            if args.command == "serve":
                self.serve(app)
            elif args.command == "install":
                plist = self.install()
                print(f"Installed: {url} starts at login and restarts if it stops.\nAgent: {plist}\nLogs: {self.log_file}")
            elif args.command == "uninstall":
                launchd.uninstall(self.label)
                print("Uninstalled: it no longer starts on its own. The data is untouched.")
            elif args.command == "restart":
                launchd.restart(self.label)
                print(f"Restarted: {url}")
            else:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = Path(args.rest[0]) if args.rest else Path(f"{self.name}-export-{stamp}.zip")
                print(f"Exported to {data.export_data(self.data_dir, target)}")
        except (launchd.LaunchdError, data.DataFormatError) as e:
            raise SystemExit(str(e)) from e


def _find_project(start: Path) -> Path:
    for folder in (start, *start.parents):
        if (folder / "pyproject.toml").exists():
            return folder
    raise SystemExit("No pyproject.toml here or above: run install from the app's project.")
