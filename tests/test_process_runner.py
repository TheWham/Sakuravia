"""Tests for guarded external command execution."""

from __future__ import annotations

import sys
import unittest

from app.services.process_runner import ProcessExecutionError, ProcessRunner


class ProcessRunnerTests(unittest.TestCase):
    """Cover timeout handling for yt-dlp and ffmpeg style commands."""

    def test_run_raises_clear_error_when_command_times_out(self) -> None:
        """A stuck child process should fail fast instead of freezing a task forever."""
        runner = ProcessRunner(default_timeout_seconds=1)

        with self.assertRaisesRegex(ProcessExecutionError, "超过 1 秒"):
            runner.run([sys.executable, "-c", "import time; time.sleep(5)"])
