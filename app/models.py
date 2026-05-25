"""Shared enums and data models used by the web layer and services."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _load_shanghai_timezone() -> tzinfo:
    """Return the Shanghai timezone, with a Windows-safe fixed-offset fallback."""
    try:
        return ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        # Some bare Windows Python installs do not ship the IANA tz database.
        # China Standard Time has no daylight-saving switch for this app's use case.
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


_SHANGHAI_TIMEZONE = _load_shanghai_timezone()


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
    SKIPPED = "SKIPPED"


class BiliEventStatus(str, Enum):
    """Processing states for Bilibili @ mention events."""

    NEW = "NEW"
    IGNORED = "IGNORED"
    TASK_CREATED = "TASK_CREATED"
    TASK_SUCCESS = "TASK_SUCCESS"
    TASK_FAILED = "TASK_FAILED"
    FAILED = "FAILED"


class BiliDeliveryStatus(str, Enum):
    """Mail delivery state for one Bilibili mention event."""

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
    send_mail: bool
    retry_count: int
    last_checkpoint: str
    error_message: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        """Convert enums to plain strings so FastAPI can serialize cleanly."""
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["mail_status"] = self.mail_status.value
        return payload


@dataclass(slots=True)
class BiliEventRecord:
    """Database record for one Bilibili notification item."""

    id: int
    notification_id: str
    comment_id: str
    sender_mid: str
    sender_name: str
    content: str
    source_url: str
    bvid: str
    task_id: int | None
    recipient_email: str
    status: BiliEventStatus
    delivery_status: BiliDeliveryStatus
    delivered_at: str
    error_message: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        """Convert enum fields to a plain JSON-friendly payload."""
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["delivery_status"] = self.delivery_status.value
        return payload


@dataclass(slots=True)
class BiliUserEmailRecord:
    """Local binding between a Bilibili user uid and an email address."""

    id: int
    uid: str
    username: str
    email: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        """Expose a stable JSON shape for the local management page."""
        return asdict(self)


def utc_now_text() -> str:
    """Return current Shanghai time in the text format stored by SQLite.

    The function name is kept for compatibility with older call sites. New
    records should show local Shanghai time directly on the page, so the stored
    value intentionally omits the UTC-only `Z` suffix.
    """
    return datetime.now(_SHANGHAI_TIMEZONE).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def path_to_text(value: Path | None) -> str:
    """Normalize optional paths before they are persisted in the database."""
    if value is None:
        return ""
    return str(value)
