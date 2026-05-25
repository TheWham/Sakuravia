"""Small utility helpers shared by multiple modules."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse


BVID_PATTERN = re.compile(r"(BV[0-9A-Za-z]+)")
BILIBILI_VIDEO_URL_PATTERN = re.compile(r"https?://(?:www\.)?bilibili\.com/video/(BV[0-9A-Za-z]+)(?:[^\s，。；、]*)?")


class ValidationError(ValueError):
    """Raised when the user input cannot be turned into a valid Bilibili video target."""


def normalize_bilibili_source(source: str) -> str:
    """Accept a BV id or a normal Bilibili video URL and return the canonical BV id."""
    text = source.strip()
    if not text:
        raise ValidationError("请输入 BV 号或 B 站视频链接。")

    match = BVID_PATTERN.search(text)
    if match:
        return match.group(1)

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("仅支持 BV 号或标准 B 站视频链接。")
    if "bilibili.com" not in parsed.netloc and "b23.tv" not in parsed.netloc:
        raise ValidationError("链接不是有效的 B 站视频地址。")
    raise ValidationError("未能从链接中解析出 BV 号。")


def extract_bilibili_video_source(text: str) -> tuple[str, str] | None:
    """Extract the first Bilibili video URL or BV id from free-form text."""
    url_match = BILIBILI_VIDEO_URL_PATTERN.search(text)
    if url_match:
        return url_match.group(0), url_match.group(1)

    bvid_match = BVID_PATTERN.search(text)
    if bvid_match:
        bvid = bvid_match.group(1)
        return bvid, bvid
    return None


def sanitize_filename(title: str, limit: int = 80) -> str:
    """Remove Windows-illegal characters and keep filenames short enough for daily use."""
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", title).strip().strip(".")
    compact = re.sub(r"\s+", "_", cleaned)
    if not compact:
        compact = "untitled"
    return compact[:limit]


def read_json_file(file_path: Path) -> dict[str, object]:
    """Load a UTF-8 json file created by subprocess helpers."""
    return json.loads(file_path.read_text(encoding="utf-8"))
