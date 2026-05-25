"""Resolve user input into Bilibili metadata and subtitles."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import AppConfig
from ..models import TranscriptResult, TranscriptSegment, VideoMetadata
from ..utils import normalize_bilibili_source
from .process_runner import ProcessRunner


class BiliResolverService:
    """Encapsulate all yt-dlp metadata fetching and subtitle extraction work."""

    def __init__(self, config: AppConfig, process_runner: ProcessRunner) -> None:
        self._config = config
        self._process_runner = process_runner

    def normalize_source(self, source: str) -> str:
        """Normalize form input early so duplicate tasks can be detected reliably."""
        return normalize_bilibili_source(source)

    def fetch_metadata(self, bvid: str) -> VideoMetadata:
        """Ask yt-dlp for video metadata, subtitles, and fallback information."""
        url = f"https://www.bilibili.com/video/{bvid}"
        output = self._process_runner.run(
            [
                self._config.yt_dlp_bin,
                "--dump-single-json",
                "--no-playlist",
                url,
            ]
        )
        payload = json.loads(output)

        subtitle_candidates: list[dict[str, str]] = []
        subtitles = payload.get("subtitles") or {}
        automatic_captions = payload.get("automatic_captions") or {}
        for source_name, entries in (("subtitles", subtitles), ("automatic_captions", automatic_captions)):
            for lang, items in entries.items():
                for item in items:
                    subtitle_candidates.append(
                        {
                            "lang": str(lang),
                            "source": source_name,
                            "url": str(item.get("url", "")),
                            "ext": str(item.get("ext", "")),
                        }
                    )

        return VideoMetadata(
            bvid=bvid,
            title=str(payload.get("title", bvid)),
            uploader=str(payload.get("uploader", "")),
            duration=int(payload.get("duration") or 0),
            webpage_url=str(payload.get("webpage_url", url)),
            description=str(payload.get("description", "")),
            tags=self._parse_tags(payload.get("tags")),
            subtitle_candidates=subtitle_candidates,
        )

    def fetch_subtitles(self, metadata: VideoMetadata) -> TranscriptResult | None:
        """Download the most useful Chinese subtitle track if one exists."""
        candidates = [item for item in metadata.subtitle_candidates if item.get("lang", "").startswith("zh")]
        if not candidates:
            return None

        target_dir = self._config.tmp_dir / f"subtitle_{metadata.bvid}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_prefix = target_dir / metadata.bvid

        self._process_runner.run(
            [
                self._config.yt_dlp_bin,
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                "zh.*,zh-CN,zh-Hans",
                "--sub-format",
                "json3/vtt/best",
                "-o",
                str(target_prefix),
                metadata.webpage_url,
            ]
        )

        subtitle_files = sorted(target_dir.glob("*.json3")) + sorted(target_dir.glob("*.vtt"))
        for subtitle_file in subtitle_files:
            transcript = self._read_subtitle_file(subtitle_file)
            if transcript.full_text.strip():
                return transcript
        return None

    def _read_subtitle_file(self, file_path: Path) -> TranscriptResult:
        """Parse the two formats yt-dlp most commonly leaves behind for Bilibili."""
        if file_path.suffix == ".json3":
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            segments: list[TranscriptSegment] = []
            texts: list[str] = []
            for event in payload.get("events", []):
                parts = event.get("segs") or []
                text = "".join(part.get("utf8", "") for part in parts).strip()
                if not text:
                    continue
                start = (event.get("tStartMs") or 0) / 1000
                duration = (event.get("dDurationMs") or 0) / 1000
                segments.append(TranscriptSegment(start=start, end=start + duration, text=text))
                texts.append(text)
            return TranscriptResult(source="official_subtitle", full_text="\n".join(texts), segments=segments)

        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        segments = []
        texts = []
        pending_text: list[str] = []
        start = None
        end = None
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == "WEBVTT":
                continue
            if "-->" in stripped:
                if pending_text:
                    text = " ".join(pending_text).strip()
                    segments.append(TranscriptSegment(start=start, end=end, text=text))
                    texts.append(text)
                    pending_text = []
                start_text, end_text = [part.strip() for part in stripped.split("-->", 1)]
                start = self._parse_vtt_time(start_text)
                end = self._parse_vtt_time(end_text)
                continue
            if stripped.isdigit():
                continue
            pending_text.append(stripped)
        if pending_text:
            text = " ".join(pending_text).strip()
            segments.append(TranscriptSegment(start=start, end=end, text=text))
            texts.append(text)
        return TranscriptResult(source="official_subtitle", full_text="\n".join(texts), segments=segments)

    def _parse_vtt_time(self, text: str) -> float:
        """Convert a VTT timestamp into floating-point seconds."""
        normalized = text.replace(",", ".")
        hour_text, minute_text, second_text = normalized.split(":")
        return int(hour_text) * 3600 + int(minute_text) * 60 + float(second_text)

    def _parse_tags(self, raw_tags: object) -> list[str]:
        """Normalize yt-dlp tag variants into a stable list for summary prompts."""
        if not isinstance(raw_tags, list):
            return []

        tags: list[str] = []
        for item in raw_tags:
            if isinstance(item, str):
                tag = item.strip()
            elif isinstance(item, dict):
                tag = str(item.get("tag_name") or item.get("name") or "").strip()
            else:
                tag = ""
            if tag:
                tags.append(tag)
        return tags
