"""Tests for configurable ASR transcription providers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.services.http_client import HttpRequestError
from app.services.transcription import OssAudioStorage, TranscriptionService, UploadedAudio


class TranscriptionServiceTests(unittest.TestCase):
    """Cover provider behavior without calling real ASR APIs."""

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
            service = TranscriptionService(
                _build_config(Path(temp_dir), asr_provider="groq"),
                http_client,
                audio_service,
            )

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
            service = TranscriptionService(
                _build_config(Path(temp_dir), asr_provider="groq"),
                http_client,
                audio_service,
            )

            result = service.transcribe_audio(audio_path)

            self.assertEqual(result.full_text, "分片转写内容")
            self.assertEqual(audio_service.split_count, 1)
            self.assertEqual(http_client.call_count, 3)

    def test_aliyun_paraformer_uses_oss_url_and_cleans_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "audio.m4a"
            audio_path.write_bytes(b"audio")
            http_client = FakeHttpClient(
                post_json_responses=[
                    {"output": {"task_id": "task-1"}},
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "file_url": "https://bucket.example.com/asr/audio.m4a",
                                    "transcription_url": "https://result.example.com/result.json",
                                    "subtask_status": "SUCCEEDED",
                                }
                            ],
                        }
                    },
                ],
                get_json_responses=[
                    {
                        "transcripts": [
                            {"text": "第一段转写"},
                            {"sentences": [{"text": "第二句"}, {"text": "第三句"}]},
                        ]
                    }
                ],
            )
            storage = FakeAudioStorageUrl("asr/test/audio.m4a", "https://bucket.example.com/asr/test/audio.m4a")
            service = TranscriptionService(
                _build_config(Path(temp_dir)),
                http_client,
                FakeAudioService(audio_path),
                storage,
            )

            result = service.transcribe_audio(audio_path)

            self.assertEqual(result.full_text, "第一段转写\n第二句第三句")
            self.assertEqual(storage.uploaded_paths, [audio_path])
            self.assertEqual(storage.deleted_keys, ["asr/test/audio.m4a"])
            self.assertEqual(http_client.post_json_calls[0]["payload"]["input"]["file_urls"], [storage.file_url])
            self.assertEqual(http_client.post_json_calls[0]["headers"]["X-DashScope-Async"], "enable")
            self.assertEqual(http_client.get_json_urls, ["https://result.example.com/result.json"])

    def test_aliyun_paraformer_failed_task_keeps_provider_message_and_cleans_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "audio.m4a"
            audio_path.write_bytes(b"audio")
            http_client = FakeHttpClient(
                post_json_responses=[
                    {"output": {"task_id": "task-1"}},
                    {"output": {"task_status": "FAILED", "message": "download failed"}},
                ],
            )
            storage = FakeAudioStorageUrl("asr/test/audio.m4a", "https://bucket.example.com/asr/test/audio.m4a")
            service = TranscriptionService(
                _build_config(Path(temp_dir)),
                http_client,
                FakeAudioService(audio_path),
                storage,
            )

            with self.assertRaisesRegex(HttpRequestError, "download failed"):
                service.transcribe_audio(audio_path)

            self.assertEqual(storage.deleted_keys, ["asr/test/audio.m4a"])

    def test_aliyun_paraformer_empty_result_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "audio.m4a"
            audio_path.write_bytes(b"audio")
            http_client = FakeHttpClient(
                post_json_responses=[
                    {"output": {"task_id": "task-1"}},
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "transcription_url": "https://result.example.com/result.json",
                                    "subtask_status": "SUCCEEDED",
                                }
                            ],
                        }
                    },
                ],
                get_json_responses=[{"transcripts": [{"text": ""}]}],
            )
            storage = FakeAudioStorageUrl("asr/test/audio.m4a", "https://bucket.example.com/asr/test/audio.m4a")
            service = TranscriptionService(
                _build_config(Path(temp_dir)),
                http_client,
                FakeAudioService(audio_path),
                storage,
            )

            with self.assertRaisesRegex(HttpRequestError, "空转写结果"):
                service.transcribe_audio(audio_path)

            self.assertEqual(storage.deleted_keys, ["asr/test/audio.m4a"])

    def test_aliyun_paraformer_running_task_can_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "audio.m4a"
            audio_path.write_bytes(b"audio")
            http_client = FakeHttpClient(
                post_json_responses=[
                    {"output": {"task_id": "task-1"}},
                    {"output": {"task_status": "RUNNING"}},
                ],
            )
            storage = FakeAudioStorageUrl("asr/test/audio.m4a", "https://bucket.example.com/asr/test/audio.m4a")
            service = TranscriptionService(
                _build_config(Path(temp_dir), aliyun_asr_timeout_seconds=0),
                http_client,
                FakeAudioService(audio_path),
                storage,
            )

            with self.assertRaisesRegex(HttpRequestError, "任务超时"):
                service.transcribe_audio(audio_path)

            self.assertEqual(storage.deleted_keys, ["asr/test/audio.m4a"])

    def test_oss_storage_uses_signed_url_when_public_base_url_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "audio.m4a"
            audio_path.write_bytes(b"audio")
            storage = OssAudioStorage(_build_config(Path(temp_dir), aliyun_oss_public_base_url=""))
            fake_bucket = FakeOssBucket()
            storage._bucket = fake_bucket

            uploaded = storage.upload_audio(audio_path)
            storage.delete_audio(uploaded.object_key)

            self.assertEqual(uploaded.file_url, "https://signed.example.com/audio.m4a?expires=3600")
            self.assertEqual(fake_bucket.uploaded_files, [(uploaded.object_key, str(audio_path), {"Content-Type": "audio/mp4"})])
            self.assertEqual(fake_bucket.signed_requests, [("GET", uploaded.object_key, 3600)])
            self.assertEqual(fake_bucket.deleted_keys, [uploaded.object_key])


class FakeHttpClient:
    """Return scripted responses so provider behavior stays deterministic."""

    def __init__(
        self,
        multipart_responses: list[str | HttpRequestError] | None = None,
        post_json_responses: list[dict[str, object] | HttpRequestError] | None = None,
        get_json_responses: list[dict[str, object] | HttpRequestError] | None = None,
    ) -> None:
        self._multipart_responses = multipart_responses or []
        self._post_json_responses = post_json_responses or []
        self._get_json_responses = get_json_responses or []
        self.call_count = 0
        self.post_json_calls: list[dict[str, object]] = []
        self.get_json_urls: list[str] = []

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
        response = self._multipart_responses.pop(0)
        if isinstance(response, HttpRequestError):
            raise response
        return response

    def post_json(self, url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        """Return the next JSON response and record request details."""
        self.post_json_calls.append({"url": url, "headers": headers, "payload": payload})
        response = self._post_json_responses.pop(0)
        if isinstance(response, HttpRequestError):
            raise response
        return response

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, object]:
        """Return the next downloaded JSON result document."""
        self.get_json_urls.append(url)
        response = self._get_json_responses.pop(0)
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


class FakeAudioStorageUrl:
    """Pretend to publish audio through OSS and record cleanup."""

    def __init__(self, object_key: str, file_url: str) -> None:
        self.object_key = object_key
        self.file_url = file_url
        self.uploaded_paths: list[Path] = []
        self.deleted_keys: list[str] = []

    def upload_audio(self, file_path: Path) -> UploadedAudio:
        """Return a fixed URL for the uploaded audio file."""
        self.uploaded_paths.append(file_path)
        return UploadedAudio(object_key=self.object_key, file_url=self.file_url)

    def delete_audio(self, object_key: str) -> None:
        """Record temporary object cleanup."""
        self.deleted_keys.append(object_key)


class FakeOssBucket:
    """Small stand-in for oss2.Bucket when testing signed URL behavior."""

    def __init__(self) -> None:
        self.uploaded_files: list[tuple[str, str, dict[str, str]]] = []
        self.signed_requests: list[tuple[str, str, int]] = []
        self.deleted_keys: list[str] = []

    def put_object_from_file(
        self,
        object_key: str,
        file_path: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Record the object key and source path instead of uploading."""
        self.uploaded_files.append((object_key, file_path, headers or {}))

    def sign_url(self, method: str, object_key: str, expires: int) -> str:
        """Return a deterministic signed URL for assertions."""
        self.signed_requests.append((method, object_key, expires))
        return "https://signed.example.com/audio.m4a?expires=3600"

    def delete_object(self, object_key: str) -> None:
        """Record object cleanup."""
        self.deleted_keys.append(object_key)


def _build_config(
    root: Path,
    asr_provider: str = "aliyun_paraformer",
    aliyun_asr_timeout_seconds: int = 2,
    aliyun_oss_public_base_url: str = "https://bucket.oss-cn-beijing.aliyuncs.com",
) -> AppConfig:
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
        asr_provider=asr_provider,
        aliyun_dashscope_api_key="dashscope-key",
        aliyun_asr_model="paraformer-v2",
        aliyun_oss_access_key_id="oss-key",
        aliyun_oss_access_key_secret="oss-secret",
        aliyun_oss_endpoint="https://oss-cn-beijing.aliyuncs.com",
        aliyun_oss_bucket="bucket",
        aliyun_oss_public_base_url=aliyun_oss_public_base_url,
        aliyun_oss_signed_url_expires_seconds=3600,
        aliyun_asr_poll_interval_seconds=0,
        aliyun_asr_timeout_seconds=aliyun_asr_timeout_seconds,
    )
