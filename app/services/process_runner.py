"""Subprocess helpers used for yt-dlp and ffmpeg invocations."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path


class ProcessExecutionError(RuntimeError):
    """Raised when an external command exits with a non-zero status."""


class ProcessRunner:
    """Wrap subprocess execution so command errors surface with useful context."""

    def __init__(self, default_timeout_seconds: int = 900) -> None:
        self._default_timeout_seconds = default_timeout_seconds
        self._logger = logging.getLogger("mysakura.process")

    def run(self, args: list[str], cwd: Path | None = None, timeout_seconds: int | None = None) -> str:
        """Execute a command and return stdout as text.

        外部命令是整条链路里最容易“无声卡住”的部分，例如 yt-dlp 等待网络、
        ffmpeg 等待文件句柄释放。这里统一加超时和耗时日志，业务服务只关心
        成功输出或明确失败原因。
        """
        timeout = timeout_seconds or self._default_timeout_seconds
        started_at = time.monotonic()
        command_name = Path(args[0]).name if args else "external-command"
        self._logger.warning("Starting %s with timeout=%ss", command_name, timeout)
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started_at
            self._logger.error("%s timed out after %.1fs", command_name, elapsed)
            raise ProcessExecutionError(f"{command_name} 执行超过 {timeout} 秒，已终止。") from exc

        elapsed = time.monotonic() - started_at
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "external command failed"
            self._logger.error("%s failed after %.1fs: %s", command_name, elapsed, message[:500])
            raise ProcessExecutionError(message)
        self._logger.warning("%s finished in %.1fs", command_name, elapsed)
        return result.stdout
