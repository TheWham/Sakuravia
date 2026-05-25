"""Configuration loading for the local assistant."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = BASE_DIR / ".env"


def _parse_env_file(env_path: Path) -> dict[str, str]:
    """Read a simple .env file without pulling an extra dependency."""
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _get_setting(name: str, file_values: dict[str, str], default: str = "") -> str:
    """Environment variables override the values defined in the local .env file."""
    return os.getenv(name, file_values.get(name, default))


@dataclass(slots=True)
class AppConfig:
    """Runtime settings used across the whole application."""

    app_host: str
    app_port: int
    sqlite_path: Path
    output_dir: Path
    audio_dir: Path
    tmp_dir: Path
    yt_dlp_bin: str
    ffmpeg_bin: str
    groq_api_key: str
    groq_asr_model: str
    deepseek_base_url: str
    deepseek_api_key: str
    deepseek_model: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_ssl: bool
    mail_from: str
    mail_to: str
    bili_enable_listener: bool = False
    bili_cookie: str = ""
    bili_self_mid: str = ""
    bili_poll_interval_seconds: int = 60
    bili_request_timeout_seconds: int = 15

    @classmethod
    def load(cls, env_path: Path | None = None) -> "AppConfig":
        """Load configuration and resolve all file-system locations under the workspace."""
        target_env = env_path or DEFAULT_ENV_PATH
        file_values = _parse_env_file(target_env)

        sqlite_path = BASE_DIR / _get_setting("SQLITE_PATH", file_values, "data/tasks.db")
        output_dir = BASE_DIR / _get_setting("OUTPUT_DIR", file_values, "data/output")
        audio_dir = BASE_DIR / "data/audio"
        tmp_dir = BASE_DIR / "data/tmp"

        return cls(
            app_host=_get_setting("APP_HOST", file_values, "127.0.0.1"),
            app_port=int(_get_setting("APP_PORT", file_values, "8000")),
            sqlite_path=sqlite_path,
            output_dir=output_dir,
            audio_dir=audio_dir,
            tmp_dir=tmp_dir,
            yt_dlp_bin=_get_setting("YT_DLP_BIN", file_values, "yt-dlp"),
            ffmpeg_bin=_get_setting("FFMPEG_BIN", file_values, "ffmpeg"),
            groq_api_key=_get_setting("GROQ_API_KEY", file_values),
            groq_asr_model=_get_setting("GROQ_ASR_MODEL", file_values, "whisper-large-v3-turbo"),
            deepseek_base_url=_get_setting("DEEPSEEK_BASE_URL", file_values, "https://api.deepseek.com"),
            deepseek_api_key=_get_setting("DEEPSEEK_API_KEY", file_values),
            deepseek_model=_get_setting("DEEPSEEK_MODEL", file_values, "deepseek-chat"),
            smtp_host=_get_setting("SMTP_HOST", file_values),
            smtp_port=int(_get_setting("SMTP_PORT", file_values, "465")),
            smtp_username=_get_setting("SMTP_USERNAME", file_values),
            smtp_password=_get_setting("SMTP_PASSWORD", file_values),
            smtp_use_ssl=_get_setting("SMTP_USE_SSL", file_values, "true").lower() == "true",
            mail_from=_get_setting("MAIL_FROM", file_values),
            mail_to=_get_setting("MAIL_TO", file_values),
            bili_enable_listener=_get_setting("BILI_ENABLE_LISTENER", file_values, "false").lower() == "true",
            bili_cookie=_get_setting("BILI_COOKIE", file_values),
            bili_self_mid=_get_setting("BILI_SELF_MID", file_values),
            bili_poll_interval_seconds=int(_get_setting("BILI_POLL_INTERVAL_SECONDS", file_values, "60")),
            bili_request_timeout_seconds=int(_get_setting("BILI_REQUEST_TIMEOUT_SECONDS", file_values, "15")),
        )

    def ensure_directories(self) -> None:
        """Create required folders before the first task is persisted or written."""
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
