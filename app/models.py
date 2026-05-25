"""Shared enums and data models used by the web layer and services."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class TaskStatus(str, Enum):
    """Visible processing states shown on the page and stored in SQLite."""

    PENDING = "PENDING"
    RESOLVING_VIDEO = "RESOLVING_VIDEO"
    FETCHING_SUBTITLE = "FETCHING_SUBTITLE"
    DOWNLOADING_AUDIO = "DOWNLOADING_AUDIO"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    WRITING_MARKDOWN = "WRITING_MARKDOWN"
    SENDING_MAIL = "SENDING_MAIL"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class MailStatus(str, Enum):
    """Mail delivery status tracked separately from the core task status."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


@dataclass(slots=True)
class VideoMetadata:
    """Structured metadata parsed from yt-dlp json output."""

    bvid: str
    title: str
    uploader: str
    duration: int
    webpage_url: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    subtitle_candidates: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class VideoPart:
    """One selectable entry from a Bilibili single video, multi-part video, or collection."""

    index: int
    title: str
    duration: int
    url: str

    def to_dict(self) -> dict[str, object]:
        """Expose a stable JSON shape for the page selection panel."""
        return asdict(self)


@dataclass(slots=True)
class TranscriptSegment:
    """One ordered chunk of transcript text."""

    start: float | None
    end: float | None
    text: str


@dataclass(slots=True)
class TranscriptResult:
    """Transcript returned from subtitles or ASR."""

    source: str
    full_text: str
    segments: list[TranscriptSegment] = field(default_factory=list)


@dataclass(slots=True)
class SummaryResult:
    """Final Markdown content plus a lightweight highlight list for UI use."""

    markdown: str
    title: str
    highlights: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TaskRecord:
    """Database record mapped into a Python object for API responses."""

    id: int
    source_input: str
    bvid: str
    video_title: str
    status: TaskStatus
    subtitle_source: str
    audio_file_path: str
    markdown_file_path: str
    markdown_content: str
    mail_status: MailStatus
    error_message: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        """Convert enums to plain strings so FastAPI can serialize cleanly."""
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["mail_status"] = self.mail_status.value
        return payload


def utc_now_text() -> str:
    """Store timestamps in a stable textual form to keep SQLite usage simple."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def path_to_text(value: Path | None) -> str:
    """Normalize optional paths before they are persisted in the database."""
    if value is None:
        return ""
    return str(value)
