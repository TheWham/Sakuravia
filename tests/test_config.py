"""Configuration loading tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import BASE_DIR, AppConfig


class AppConfigTests(unittest.TestCase):
    """Cover deployment-only configuration fields."""

    def test_load_resolves_relative_yt_dlp_cookies_file_under_project(self) -> None:
        """Relative cookies paths should work the same way as data paths."""
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("YT_DLP_COOKIES_FILE=data/bilibili-cookies.txt\n", encoding="utf-8")

            config = AppConfig.load(env_path)

            self.assertEqual(config.yt_dlp_cookies_file, BASE_DIR / "data/bilibili-cookies.txt")


if __name__ == "__main__":
    unittest.main()
