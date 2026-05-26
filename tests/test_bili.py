"""Bilibili metadata parsing tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.models import VideoMetadata
from app.services.bili import BiliResolverService


class VideoMetadataTests(unittest.TestCase):
    """Cover the extra context used by the summary prompt."""

    def test_video_metadata_keeps_description_and_tags(self) -> None:
        metadata = VideoMetadata(
            bvid="BV1test",
            title="测试视频",
            uploader="测试UP",
            duration=60,
            webpage_url="https://www.bilibili.com/video/BV1test",
            description="视频简介",
            tags=["AI", "Prompt"],
        )

        self.assertEqual(metadata.description, "视频简介")
        self.assertEqual(metadata.tags, ["AI", "Prompt"])


class BiliResolverMetadataTests(unittest.TestCase):
    """Verify yt-dlp JSON fields are preserved for better summaries."""

    def test_fetch_metadata_extracts_description_and_tags(self) -> None:
        payload = {
            "title": "Anthropic 最新分享：Claude Code 提示词缓存的最佳实践",
            "uploader": "code秘密花园",
            "duration": 321,
            "webpage_url": "https://www.bilibili.com/video/BV1dRRyBREPM",
            "description": "Anthropic 工程师公开的 Claude Code 缓存经验",
            "tags": ["Claude Code", {"tag_name": "提示词缓存"}, {"name": "AI编程"}],
            "subtitles": {
                "zh-CN": [
                    {
                        "url": "https://example.com/subtitle.json3",
                        "ext": "json3",
                    }
                ]
            },
        }
        service = BiliResolverService(_config(), FakeProcessRunner(payload))

        metadata = service.fetch_metadata("BV1dRRyBREPM")

        self.assertEqual(metadata.description, "Anthropic 工程师公开的 Claude Code 缓存经验")
        self.assertEqual(metadata.tags, ["Claude Code", "提示词缓存", "AI编程"])
        self.assertEqual(metadata.webpage_url, "https://www.bilibili.com/video/BV1dRRyBREPM")
        self.assertEqual(metadata.subtitle_candidates[0]["lang"], "zh-CN")

    def test_inspect_parts_returns_selectable_entries(self) -> None:
        payload = {
            "title": "英语口语合集",
            "entries": [
                {"title": "At the Airport", "duration": 300},
                {"title": "At the Hotel", "duration": 420},
            ],
        }
        service = BiliResolverService(_config(), FakeProcessRunner(payload))

        bvid, title, parts = service.inspect_parts("BV1X54y1p7Dd")

        self.assertEqual(bvid, "BV1X54y1p7Dd")
        self.assertEqual(title, "英语口语合集")
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].title, "At the Airport")
        self.assertIn("p=1", parts[0].url)
        self.assertIn("p=2", parts[1].url)

    def test_metadata_fetch_uses_configured_cookies_file(self) -> None:
        """yt-dlp should receive cookies.txt when ECS needs B 站 login state."""
        payload = {
            "title": "测试视频",
            "webpage_url": "https://www.bilibili.com/video/BV1cookie",
        }
        config = _config()
        config.yt_dlp_cookies_file = Path("data/bilibili-cookies.txt")
        runner = FakeProcessRunner(payload)
        service = BiliResolverService(config, runner)

        service.fetch_metadata("BV1cookie")

        self.assertIn("--cookies", runner.args)
        self.assertEqual(
            runner.args[runner.args.index("--cookies") + 1],
            str(config.yt_dlp_cookies_file),
        )


class FakeProcessRunner:
    """Return a stable yt-dlp JSON payload."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.args: list[str] = []

    def run(self, args: list[str], cwd: Path | None = None, timeout_seconds: int | None = None) -> str:
        self.args = args
        return json.dumps(self.payload, ensure_ascii=False)


def _config() -> AppConfig:
    """Build config fields required by the resolver."""
    temp_root = Path(tempfile.gettempdir()) / "mysakura-test"
    return AppConfig(
        app_host="127.0.0.1",
        app_port=8000,
        sqlite_path=temp_root / "tasks.db",
        output_dir=temp_root / "output",
        audio_dir=temp_root / "audio",
        tmp_dir=temp_root / "tmp",
        yt_dlp_bin="yt-dlp",
        ffmpeg_bin="ffmpeg",
        groq_api_key="",
        groq_asr_model="whisper-large-v3-turbo",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_api_key="",
        deepseek_model="deepseek-chat",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="",
        smtp_password="",
        smtp_use_ssl=True,
        mail_from="",
        mail_to="",
    )
