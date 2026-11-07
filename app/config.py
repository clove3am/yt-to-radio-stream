"""Application configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
import secrets
import shlex
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

TRUTHY = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in TRUTHY


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    return max(minimum, min(maximum, value))


def _read_or_create_secret(path: Path, length: int = 32) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        pass

    value = secrets.token_urlsafe(length)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    static_dir: Path
    db_path: Path
    host: str
    port: int
    log_level: str
    on_demand_idle_seconds: int
    on_demand_start_timeout: int

    icecast_managed: bool
    icecast_host: str
    icecast_port: int
    icecast_source_password: str
    icecast_admin_password: str
    icecast_binary: str
    icecast_webroot: str
    icecast_adminroot: str

    ffmpeg_binary: str
    ytdlp_binary: str
    ytdlp_extra_args: list[str] = field(default_factory=list)
    cookies_file: Path | None = None

    app_username: str = "admin"
    app_password: str | None = None

    @property
    def icecast_url(self) -> str:
        return f"http://{self.icecast_host}:{self.icecast_port}"

    @property
    def icecast_config_path(self) -> Path:
        return self.data_dir / "icecast.xml"

    @property
    def access_log_path(self) -> Path:
        return self.data_dir / "logs" / "icecast-access.log"

    @property
    def icecast_pid_path(self) -> Path:
        return self.data_dir / "icecast.pid"

    @classmethod
    def load(cls) -> "Settings":
        static_dir = Path(__file__).resolve().parent / "static"
        data_dir = Path(os.environ.get("DATA_DIR", "data")).expanduser()
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)

        extra_args: list[str] = []
        raw_extra = os.environ.get("YTDLP_EXTRA_ARGS", "").strip()
        if raw_extra:
            try:
                extra_args = shlex.split(raw_extra)
            except ValueError as exc:
                logger.warning("Ignoring invalid YTDLP_EXTRA_ARGS: %s", exc)

        raw_cookies = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
        cookies_file = Path(raw_cookies).expanduser() if raw_cookies else None

        source_password = os.environ.get("ICECAST_SOURCE_PASSWORD", "").strip()
        if not source_password:
            source_password = _read_or_create_secret(
                data_dir / ".icecast-source-password"
            )

        admin_password = os.environ.get("ICECAST_ADMIN_PASSWORD", "").strip()
        if not admin_password:
            admin_password = _read_or_create_secret(
                data_dir / ".icecast-admin-password"
            )

        app_password = os.environ.get("APP_PASSWORD", "").strip() or None

        try:
            port = int(os.environ.get("PORT", "8080"))
        except ValueError:
            port = 8080

        try:
            icecast_port = int(os.environ.get("ICECAST_PORT", "8000"))
        except ValueError:
            icecast_port = 8000

        return cls(
            data_dir=data_dir,
            static_dir=static_dir,
            db_path=data_dir / "app.db",
            host=os.environ.get("HOST", "0.0.0.0"),
            port=port,
            log_level=os.environ.get("LOG_LEVEL", "info").strip().lower(),
            on_demand_idle_seconds=_env_int(
                "ON_DEMAND_IDLE_SECONDS", 30, minimum=0, maximum=3600
            ),
            on_demand_start_timeout=_env_int(
                "ON_DEMAND_START_TIMEOUT", 75, minimum=10, maximum=600
            ),
            icecast_managed=_env_bool("ICECAST_MANAGED", True),
            icecast_host=os.environ.get("ICECAST_HOST", "127.0.0.1").strip()
            or "127.0.0.1",
            icecast_port=icecast_port,
            icecast_source_password=source_password,
            icecast_admin_password=admin_password,
            icecast_binary=os.environ.get("ICECAST_BINARY", "icecast2"),
            icecast_webroot=os.environ.get(
                "ICECAST_WEBROOT", "/usr/share/icecast2/web"
            ),
            icecast_adminroot=os.environ.get(
                "ICECAST_ADMINROOT", "/usr/share/icecast2/admin"
            ),
            ffmpeg_binary=os.environ.get("FFMPEG_BINARY", "ffmpeg"),
            ytdlp_binary=os.environ.get("YTDLP_BINARY", "yt-dlp"),
            ytdlp_extra_args=extra_args,
            cookies_file=cookies_file,
            app_username=os.environ.get("APP_USERNAME", "admin").strip() or "admin",
            app_password=app_password,
        )
