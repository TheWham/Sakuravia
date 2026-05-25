"""Subprocess helpers used for yt-dlp and ffmpeg invocations."""

from __future__ import annotations

import subprocess
from pathlib import Path


class ProcessExecutionError(RuntimeError):
    """Raised when an external command exits with a non-zero status."""


class ProcessRunner:
    """Wrap subprocess execution so command errors surface with useful context."""

    def run(self, args: list[str], cwd: Path | None = None) -> str:
        """Execute a command and return stdout as text."""
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "external command failed"
            raise ProcessExecutionError(message)
        return result.stdout
