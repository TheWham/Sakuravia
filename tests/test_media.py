"""Tests for media probing and OSS media helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.services.media import AudioProbeService, InvalidAudioError, OssMediaStorage
from app.services.process_runner import ProcessResult


class AudioProbeServiceTests(unittest.TestCase):
    """Cover local audio validity checks without invoking ffprobe or ffmpeg."""

    def test_rejects_tiny_audio_file_before_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "tiny.m4a"
            audio_path.write_bytes(b"x")
            service = AudioProbeService(_build_config(Path(temp_dir)), FakeProcessRunner())

            with self.assertRaisesRegex(InvalidAudioError, "音频文件过小"):
                service.ensure_valid_audio(audio_path)

    def test_rejects_file_without_audio_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"x" * 4096)
            runner = FakeProcessRunner(ffprobe_stdout='{"streams":[],"format":{"duration":"10"}}')
            service = AudioProbeService(_build_config(Path(temp_dir)), runner)

            with self.assertRaisesRegex(InvalidAudioError, "未检测到音频流"):
                service.ensure_valid_audio(audio_path)

    def test_rejects_long_silent_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"x" * 4096)
            runner = FakeProcessRunner(ffmpeg_stderr="[Parsed_volumedetect] mean_volume: -60.0 dB")
            service = AudioProbeService(_build_config(Path(temp_dir)), runner)

            with self.assertRaisesRegex(InvalidAudioError, "接近静音"):
                service.ensure_valid_audio(audio_path)

    def test_rejects_fully_silent_audio_reported_as_negative_infinity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"x" * 4096)
            runner = FakeProcessRunner(ffmpeg_stderr="[Parsed_volumedetect] mean_volume: -inf dB")
            service = AudioProbeService(_build_config(Path(temp_dir)), runner)

            with self.assertRaisesRegex(InvalidAudioError, "接近静音"):
                service.ensure_valid_audio(audio_path)

    def test_accepts_non_silent_audio_without_voice_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"x" * 4096)
            runner = FakeProcessRunner(ffmpeg_stderr="[Parsed_volumedetect] mean_volume: -18.0 dB")
            service = AudioProbeService(_build_config(Path(temp_dir)), runner)

            service.ensure_valid_audio(audio_path)
            self.assertEqual(runner.run_with_result_count, 1)

    def test_accepts_normal_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"x" * 4096)
            runner = FakeProcessRunner(ffmpeg_stderr="[Parsed_volumedetect] mean_volume: -20.0 dB")
            service = AudioProbeService(_build_config(Path(temp_dir)), runner)

            service.ensure_valid_audio(audio_path)


class OssMediaStorageTests(unittest.TestCase):
    """Cover provider-facing object metadata for temporary OSS uploads."""

    def test_upload_sets_mimo_supported_audio_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.m4a"
            audio_path.write_bytes(b"audio")
            storage = OssMediaStorage(_build_config(Path(temp_dir)))
            fake_bucket = FakeOssBucket()
            storage._bucket = fake_bucket

            storage.upload_file(audio_path, "mimo/audio")

            self.assertEqual(fake_bucket.uploaded_headers[0]["Content-Type"], "audio/mp4")


class FakeProcessRunner:
    """Return ffprobe and ffmpeg output expected by AudioProbeService."""

    def __init__(
        self,
        ffprobe_stdout: str = '{"streams":[{"codec_type":"audio","duration":"12.5"}],"format":{"duration":"12.5"}}',
        ffmpeg_stderr: str = "[Parsed_volumedetect] mean_volume: -20.0 dB",
    ) -> None:
        self.ffprobe_stdout = ffprobe_stdout
        self.ffmpeg_stderr = ffmpeg_stderr
        self.run_with_result_count = 0

    def run(self, args: list[str], timeout_seconds: int | None = None) -> str:
        return self.ffprobe_stdout

    def run_with_result(self, args: list[str], timeout_seconds: int | None = None) -> ProcessResult:
        self.run_with_result_count += 1
        return ProcessResult(stdout="", stderr=self.ffmpeg_stderr)


class FakeOssBucket:
    """Record upload headers passed to oss2.Bucket."""

    def __init__(self) -> None:
        self.uploaded_headers: list[dict[str, str]] = []

    def put_object_from_file(
        self,
        object_key: str,
        file_path: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.uploaded_headers.append(headers or {})

    def sign_url(self, method: str, object_key: str, expires: int) -> str:
        return "https://signed.example.com/sample.m4a"


def _build_config(root: Path) -> AppConfig:
    """Build the minimal config needed by AudioProbeService."""
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
        aliyun_oss_access_key_id="oss-key",
        aliyun_oss_access_key_secret="oss-secret",
        aliyun_oss_endpoint="https://oss-cn-beijing.aliyuncs.com",
        aliyun_oss_bucket="bucket",
    )
