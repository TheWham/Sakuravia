"""Groq Whisper transcription service."""

from __future__ import annotations

from pathlib import Path

from ..config import AppConfig
from ..models import TranscriptResult
from .audio import SubtitleOrAudioService
from .http_client import HttpRequestError, SimpleHttpClient


class TranscriptionService:
    """Call Groq Whisper and handle chunked retries for larger audio files."""

    _RETRYABLE_UPLOAD_STATUS_CODES = {400, 413}

    def __init__(
        self,
        config: AppConfig,
        http_client: SimpleHttpClient,
        audio_service: SubtitleOrAudioService,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._audio_service = audio_service

    def transcribe_audio(self, audio_path: Path) -> TranscriptResult:
        """Use a direct upload first, then split the audio when the file is too large or rejected."""
        try:
            text = self._transcribe_file(audio_path)
            return TranscriptResult(source="asr_audio", full_text=text)
        except HttpRequestError as exc:
            if not self._should_retry_with_chunks(exc):
                raise
            chunks = self._audio_service.split_audio(audio_path)
            parts = [self._transcribe_file(chunk) for chunk in chunks]
            return TranscriptResult(source="asr_audio", full_text="\n".join(part.strip() for part in parts if part.strip()))

    def _transcribe_file(self, file_path: Path) -> str:
        """Send one audio file to Groq and return plain text."""
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {
            "Authorization": f"Bearer {self._config.groq_api_key}",
        }
        fields = {
            "model": self._config.groq_asr_model,
            "language": "zh",
            "response_format": "text",
            "temperature": "0",
        }
        last_error: HttpRequestError | None = None
        for _ in range(2):
            try:
                return self._http_client.post_multipart(url, headers, fields, "file", file_path).strip()
            except HttpRequestError as exc:
                last_error = exc
                continue
        detail = str(last_error) if last_error is not None else "未知错误"
        raise HttpRequestError(f"Groq Whisper 转写连续两次失败：{detail}") from last_error

    def _should_retry_with_chunks(self, exc: HttpRequestError) -> bool:
        """Only split audio when the provider hints that the upload itself is the problem."""
        if exc.status_code in self._RETRYABLE_UPLOAD_STATUS_CODES:
            return True
        message = str(exc).lower()
        return "file" in message and ("large" in message or "size" in message or "format" in message)
