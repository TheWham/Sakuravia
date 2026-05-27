"""Tests for Xiaomi Mimo Markdown generation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.models import TranscriptResult, VideoMetadata
from app.services.http_client import HttpRequestError
from app.services.media import UploadedMedia
from app.services.mimo import MimoNoValidAudioError, MimoSummaryService


class MimoSummaryServiceTests(unittest.TestCase):
    """Cover request shape and cleanup without calling the real Mimo API."""

    def test_text_summary_uses_official_subtitle_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            http_client = FakeHttpClient("# Mimo 字幕总结")
            service = MimoSummaryService(_build_config(Path(temp_dir)), http_client, FakeMediaStorage())

            result = service.summarize_text(_metadata(), TranscriptResult(source="official_subtitle", full_text="字幕"))

            self.assertEqual(result.markdown, "# Mimo 字幕总结")
            self.assertEqual(http_client.calls[0]["headers"]["api-key"], "mimo-key")
            content = http_client.calls[0]["payload"]["messages"][1]["content"]
            self.assertIn("转写内容：", content)
            self.assertIn("字幕", content)

    def test_audio_summary_uploads_oss_url_and_cleans_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"audio")
            storage = FakeMediaStorage()
            http_client = FakeHttpClient("# Mimo 音频总结")
            service = MimoSummaryService(_build_config(Path(temp_dir)), http_client, storage)

            result = service.summarize_audio(_metadata(), audio_path)

            self.assertEqual(result.markdown, "# Mimo 音频总结")
            self.assertEqual(storage.uploads, [(audio_path, "mimo/audio")])
            self.assertEqual(storage.deleted_keys, ["mimo/test/sample.m4a"])
            message_content = http_client.calls[0]["payload"]["messages"][1]["content"]
            self.assertEqual(message_content[0]["type"], "input_audio")
            self.assertEqual(message_content[0]["input_audio"]["data"], storage.file_url)

    def test_audio_summary_treats_no_valid_audio_response_as_retryable_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"audio")
            storage = FakeMediaStorage()
            service = MimoSummaryService(
                _build_config(Path(temp_dir)),
                FakeHttpClient("无法识别有效内容"),
                storage,
            )

            with self.assertRaisesRegex(MimoNoValidAudioError, "无有效语音"):
                service.summarize_audio(_metadata(), audio_path)

            self.assertEqual(storage.deleted_keys, ["mimo/test/sample.m4a"])

    def test_audio_summary_treats_empty_markdown_as_retryable_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"audio")
            storage = FakeMediaStorage()
            service = MimoSummaryService(_build_config(Path(temp_dir)), FakeHttpClient(""), storage)

            with self.assertRaisesRegex(MimoNoValidAudioError, "空 Markdown"):
                service.summarize_audio(_metadata(), audio_path)

            self.assertEqual(storage.deleted_keys, ["mimo/test/sample.m4a"])

    def test_audio_summary_treats_invalid_format_error_as_retryable_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"audio")
            storage = FakeMediaStorage()
            http_client = FakeHttpClient(
                HttpRequestError(
                    '{"error":{"param":"invalid audio format, only mp3/flac/m4a/wav/ogg are supported"}}',
                    status_code=400,
                )
            )
            service = MimoSummaryService(_build_config(Path(temp_dir)), http_client, storage)

            with self.assertRaisesRegex(MimoNoValidAudioError, "拒绝当前音频格式"):
                service.summarize_audio(_metadata(), audio_path)

            self.assertEqual(storage.deleted_keys, ["mimo/test/sample.m4a"])

    def test_video_summary_uses_video_url_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "sample.mp4"
            video_path.write_bytes(b"video")
            storage = FakeMediaStorage()
            http_client = FakeHttpClient("# Mimo 视频总结")
            service = MimoSummaryService(_build_config(Path(temp_dir)), http_client, storage)

            result = service.summarize_video(_metadata(), video_path)

            self.assertEqual(result.markdown, "# Mimo 视频总结")
            self.assertEqual(storage.uploads, [(video_path, "mimo/video")])
            message_content = http_client.calls[0]["payload"]["messages"][1]["content"]
            self.assertEqual(message_content[0]["type"], "video_url")
            self.assertEqual(message_content[0]["video_url"]["url"], storage.file_url)
            self.assertEqual(message_content[0]["fps"], 1.0)
            self.assertEqual(message_content[0]["media_resolution"], "default")

    def test_empty_mimo_content_keeps_provider_error_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = MimoSummaryService(_build_config(Path(temp_dir)), FakeHttpClient(""), FakeMediaStorage())

            with self.assertRaisesRegex(HttpRequestError, "内容为空"):
                service.summarize_text(_metadata(), TranscriptResult(source="official_subtitle", full_text="字幕"))


class FakeHttpClient:
    """Return one OpenAI-compatible Mimo chat response and record the request."""

    def __init__(self, content: str | HttpRequestError) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def post_json(self, url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if isinstance(self.content, HttpRequestError):
            raise self.content
        return {"choices": [{"message": {"content": self.content}}]}


class FakeMediaStorage:
    """Pretend to upload media through OSS and record cleanup."""

    def __init__(self) -> None:
        self.file_url = "https://bucket.example.com/mimo/test/sample.m4a"
        self.uploads: list[tuple[Path, str]] = []
        self.deleted_keys: list[str] = []

    def upload_file(self, file_path: Path, object_prefix: str) -> UploadedMedia:
        self.uploads.append((file_path, object_prefix))
        return UploadedMedia(object_key="mimo/test/sample.m4a", file_url=self.file_url)

    def delete_file(self, object_key: str) -> None:
        self.deleted_keys.append(object_key)


def _metadata() -> VideoMetadata:
    """Build representative video metadata for prompt assertions."""
    return VideoMetadata(
        bvid="BV1test",
        title="测试视频",
        uploader="测试UP",
        duration=120,
        webpage_url="https://www.bilibili.com/video/BV1test",
        description="测试简介",
        tags=["AI", "教程"],
    )


def _build_config(root: Path) -> AppConfig:
    """Build the minimal config needed by MimoSummaryService."""
    return AppConfig(
        app_host="127.0.0.1",
        app_port=8000,
        sqlite_path=root / "tasks.db",
        output_dir=root / "output",
        audio_dir=root / "audio",
        tmp_dir=root / "tmp",
        yt_dlp_bin="yt-dlp",
        ffmpeg_bin="ffmpeg",
        groq_api_key="",
        groq_asr_model="whisper-large-v3-turbo",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_api_key="",
        deepseek_model="deepseek-chat",
        smtp_host="",
        smtp_port=465,
        smtp_username="",
        smtp_password="",
        smtp_use_ssl=True,
        mail_from="",
        mail_to="",
        mimo_api_key="mimo-key",
        mimo_base_url="https://api.xiaomimimo.com/v1",
        mimo_model="mimo-v2.5",
        mimo_media_mode="auto",
        mimo_max_completion_tokens=4096,
        mimo_video_fps=1.0,
        mimo_video_resolution="default",
    )
