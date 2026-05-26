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

    def test_bili_poll_range_uses_new_min_and_max_values(self) -> None:
        """V3 listener should prefer randomized poll ranges over a fixed interval."""
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "BILI_POLL_MIN_SECONDS=180",
                        "BILI_POLL_MAX_SECONDS=480",
                    ]
                ),
                encoding="utf-8",
            )

            config = AppConfig.load(env_path)

            self.assertEqual(config.bili_poll_min_seconds, 180)
            self.assertEqual(config.bili_poll_max_seconds, 480)
            self.assertEqual(config.bili_poll_interval_seconds, 180)

    def test_bili_poll_range_keeps_legacy_fixed_interval(self) -> None:
        """Old .env files with only BILI_POLL_INTERVAL_SECONDS should keep working."""
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("BILI_POLL_INTERVAL_SECONDS=240\n", encoding="utf-8")

            config = AppConfig.load(env_path)

            self.assertEqual(config.bili_poll_min_seconds, 240)
            self.assertEqual(config.bili_poll_max_seconds, 240)
            self.assertEqual(config.bili_poll_interval_seconds, 240)

    def test_bili_poll_range_normalizes_unsafe_bounds(self) -> None:
        """The listener should not accept sub-minute polling or inverted ranges."""
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "BILI_POLL_MIN_SECONDS=10",
                        "BILI_POLL_MAX_SECONDS=20",
                    ]
                ),
                encoding="utf-8",
            )

            config = AppConfig.load(env_path)

            self.assertEqual(config.bili_poll_min_seconds, 60)
            self.assertEqual(config.bili_poll_max_seconds, 60)


if __name__ == "__main__":
    unittest.main()
