"""Tests for Groq Whisper transcription error handling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.services.http_client import HttpRequestError
from app.services.transcription import TranscriptionService


class TranscriptionServiceTests(unittest.TestCase):
    """Cover provider failures without calling the real Groq API."""

    def test_auth_or_quota_error_keeps_provider_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "audio.m4a"
            audio_path.write_bytes(b"audio")
            http_client = FakeHttpClient(
                [
                    HttpRequestError("invalid api key", status_code=401),
                    HttpRequestError("invalid api key", status_code=401),
                ]
            )
            audio_service = FakeAudioService(audio_path)
            service = TranscriptionService(_build_config(Path(temp_dir)), http_client, audio_service)

            with self.assertRaisesRegex(HttpRequestError, "invalid api key"):
                service.transcribe_audio(audio_path)

            self.assertEqual(http_client.call_count, 2)
            self.assertEqual(audio_service.split_count, 0)

    def test_upload_size_error_can_retry_with_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "audio.m4a"
            audio_path.write_bytes(b"audio")
            chunk_path = Path(temp_dir) / "chunk_000.m4a"
            chunk_path.write_bytes(b"chunk")
            http_client = FakeHttpClient(
                [
                    HttpRequestError("file too large", status_code=413),
                    HttpRequestError("file too large", status_code=413),
                    "分片转写内容",
                ]
            )
            audio_service = FakeAudioService(chunk_path)
            service = TranscriptionService(_build_config(Path(temp_dir)), http_client, audio_service)

            result = service.transcribe_audio(audio_path)

            self.assertEqual(result.full_text, "分片转写内容")
            self.assertEqual(audio_service.split_count, 1)
            self.assertEqual(http_client.call_count, 3)


class FakeHttpClient:
    """Return scripted responses so retry behavior stays deterministic."""

    def __init__(self, responses: list[str | HttpRequestError]) -> None:
        self._responses = responses
        self.call_count = 0

    def post_multipart(
        self,
        url: str,
        headers: dict[str, str],
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> str:
        """Return the next scripted response or raise the scripted error."""
        self.call_count += 1
        response = self._responses.pop(0)
        if isinstance(response, HttpRequestError):
            raise response
        return response


class FakeAudioService:
    """Only expose the split method used by TranscriptionService."""

    def __init__(self, chunk_path: Path) -> None:
        self._chunk_path = chunk_path
        self.split_count = 0

    def split_audio(self, audio_path: Path) -> list[Path]:
        """Return one prepared chunk and record that splitting was requested."""
        self.split_count += 1
        return [self._chunk_path]


def _build_config(root: Path) -> AppConfig:
    """Build the minimal config needed by the transcription service."""
    return AppConfig(
        app_host="127.0.0.1",
        app_port=8000,
        sqlite_path=root / "tasks.db",
        output_dir=root / "output",
        audio_dir=root / "audio",
        tmp_dir=root / "tmp",
        yt_dlp_bin="yt-dlp",
        ffmpeg_bin="ffmpeg",
        groq_api_key="test-key",
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
    )
