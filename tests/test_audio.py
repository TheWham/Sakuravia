"""Tests for yt-dlp audio download command construction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.models import VideoMetadata
from app.services.audio import SubtitleOrAudioService


class AudioDownloadTests(unittest.TestCase):
    """Cover Windows-specific safeguards around yt-dlp audio files."""

    def test_download_audio_avoids_part_rename_and_uses_ffmpeg_location(self) -> None:
        """yt-dlp should avoid .part renames and know where ffprobe lives."""
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            config = _config(root)
            metadata = VideoMetadata(
                bvid="BV1test",
                title="测试视频",
                uploader="测试UP",
                duration=60,
                webpage_url="https://www.bilibili.com/video/BV1test",
            )
            target_dir = config.audio_dir / metadata.bvid
            target_dir.mkdir(parents=True)
            stale_part = target_dir / "BV1test.m4a.part"
            stale_part.write_bytes(b"old")

            runner = FakeProcessRunner(config.audio_dir)
            audio_path = SubtitleOrAudioService(config, runner).download_audio(metadata)

            self.assertFalse(stale_part.exists())
            self.assertTrue(audio_path.name.endswith(".m4a"))
            self.assertEqual(audio_path.parent.parent, target_dir)
            self.assertTrue(audio_path.parent.name.startswith("run_"))
            self.assertIn("--no-part", runner.args)
            self.assertIn("--no-playlist", runner.args)
            self.assertIn("--force-overwrites", runner.args)
            self.assertIn("--ffmpeg-location", runner.args)
            self.assertIn(str(Path(config.ffmpeg_bin).parent), runner.args)

    def test_download_audio_uses_configured_cookies_file(self) -> None:
        """Audio fallback should use the same B 站 cookies as metadata parsing."""
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            config = _config(root)
            config.yt_dlp_cookies_file = root / "data" / "bilibili-cookies.txt"
            metadata = VideoMetadata(
                bvid="BV1cookie",
                title="测试视频",
                uploader="测试UP",
                duration=60,
                webpage_url="https://www.bilibili.com/video/BV1cookie",
            )

            runner = FakeProcessRunner(config.audio_dir)
            SubtitleOrAudioService(config, runner).download_audio(metadata)

            self.assertIn("--cookies", runner.args)
            self.assertIn(str(config.yt_dlp_cookies_file), runner.args)


class FakeProcessRunner:
    """Capture the command and create the file yt-dlp would leave behind."""

    def __init__(self, audio_dir: Path) -> None:
        self.audio_dir = audio_dir
        self.args: list[str] = []

    def run(self, args: list[str], cwd: Path | None = None, timeout_seconds: int | None = None) -> str:
        self.args = args
        output_template = Path(args[args.index("-o") + 1])
        output_template.with_suffix(".m4a").write_bytes(b"audio")
        return ""


def _config(root: Path) -> AppConfig:
    """Build the minimal runtime config needed by the audio service."""
    return AppConfig(
        app_host="127.0.0.1",
        app_port=8000,
        sqlite_path=root / "tasks.db",
        output_dir=root / "output",
        audio_dir=root / "audio",
        tmp_dir=root / "tmp",
        yt_dlp_bin="yt-dlp",
        ffmpeg_bin=str(root / "ffmpeg" / "bin" / "ffmpeg.exe"),
        groq_api_key="",
        groq_asr_model="whisper-large-v3-turbo",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_api_key="",
        deepseek_model="deepseek-chat",
        smtp_host="smtp.qq.com",
        smtp_port=465,
        smtp_username="",
        smtp_password="",
        smtp_use_ssl=True,
        mail_from="",
        mail_to="",
    )
