"""Resolve user input into Bilibili metadata and subtitles."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from ..config import AppConfig
from ..models import TranscriptResult, TranscriptSegment, VideoMetadata, VideoPart
from ..utils import normalize_bilibili_source
from .process_runner import ProcessRunner


METADATA_FETCH_TIMEOUT_SECONDS = 180
SUBTITLE_FETCH_TIMEOUT_SECONDS = 300


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
        return self.fetch_metadata_for_source(bvid, f"https://www.bilibili.com/video/{bvid}")

    def fetch_metadata_for_source(self, bvid: str, source_input: str) -> VideoMetadata:
        """Ask yt-dlp for metadata for a concrete video URL or BV id."""
        url = self._build_video_url(bvid, source_input)
        output = self._process_runner.run(
            self._build_yt_dlp_args(
                "--dump-single-json",
                "--no-playlist",
                url,
            ),
            timeout_seconds=METADATA_FETCH_TIMEOUT_SECONDS,
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

    def inspect_parts(self, source: str) -> tuple[str, str, list[VideoPart]]:
        """Return selectable entries before creating a task.

        普通单视频只会返回一个条目；多 P 或合集会返回多个条目，前端据此让用户
        手动选择要处理哪一集，避免后台误把整个列表交给 yt-dlp。
        """
        bvid = self.normalize_source(source)
        url = self._build_video_url(bvid, source)
        output = self._process_runner.run(
            self._build_yt_dlp_args(
                "--dump-single-json",
                url,
            ),
            timeout_seconds=METADATA_FETCH_TIMEOUT_SECONDS,
        )
        payload = json.loads(output)
        title = str(payload.get("title", bvid))
        parts = self._parse_video_parts(bvid, payload, url)
        return bvid, title, parts

    def fetch_subtitles(self, metadata: VideoMetadata) -> TranscriptResult | None:
        """Download the most useful Chinese subtitle track if one exists."""
        candidates = [item for item in metadata.subtitle_candidates if item.get("lang", "").startswith("zh")]
        if not candidates:
            return None

        target_dir = self._config.tmp_dir / f"subtitle_{metadata.bvid}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_prefix = target_dir / metadata.bvid

        self._process_runner.run(
            self._build_yt_dlp_args(
                "--skip-download",
                "--no-playlist",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                "zh.*,zh-CN,zh-Hans",
                "--sub-format",
                "json3/vtt/best",
                "-o",
                str(target_prefix),
                metadata.webpage_url,
            ),
            timeout_seconds=SUBTITLE_FETCH_TIMEOUT_SECONDS,
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

    def _build_yt_dlp_args(self, *args: str) -> list[str]:
        """Build yt-dlp arguments with the optional B 站 cookies file.

        云服务器 IP 容易触发 B 站 412 风控；cookies.txt 只作为 yt-dlp 的
        登录态输入，不进入页面，也不和 B 站 @ 监听用的 BILI_COOKIE 混用。
        """
        command = [self._config.yt_dlp_bin]
        if self._config.yt_dlp_cookies_file is not None:
            command.extend(["--cookies", str(self._config.yt_dlp_cookies_file)])
        command.extend(args)
        return command

    def _parse_video_parts(self, bvid: str, payload: dict[str, object], fallback_url: str) -> list[VideoPart]:
        """Normalize yt-dlp playlist/page entries into UI-friendly choices."""
        entries = payload.get("entries")
        if isinstance(entries, list) and entries:
            parts: list[VideoPart] = []
            for index, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict):
                    continue
                entry_title = str(entry.get("title") or entry.get("alt_title") or f"第 {index} 集")
                entry_duration = int(entry.get("duration") or 0)
                entry_url = self._normalize_entry_url(bvid, entry, fallback_url, index)
                parts.append(VideoPart(index=index, title=entry_title, duration=entry_duration, url=entry_url))
            if parts:
                return parts

        return [
            VideoPart(
                index=1,
                title=str(payload.get("title", bvid)),
                duration=int(payload.get("duration") or 0),
                url=str(payload.get("webpage_url") or fallback_url),
            )
        ]

    def _normalize_entry_url(self, bvid: str, entry: dict[str, object], fallback_url: str, index: int) -> str:
        """Build a concrete URL for one playlist entry."""
        raw_url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
        if raw_url.startswith("http"):
            return raw_url
        return self._with_page_index(fallback_url or f"https://www.bilibili.com/video/{bvid}", index)

    def _build_video_url(self, bvid: str, source_input: str) -> str:
        """Preserve user-selected query parameters when a concrete page URL is provided."""
        text = source_input.strip()
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return text
        return f"https://www.bilibili.com/video/{bvid}"

    def _with_page_index(self, url: str, index: int) -> str:
        """Attach or replace the Bilibili page index used for multi-part videos."""
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query["p"] = [str(index)]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
