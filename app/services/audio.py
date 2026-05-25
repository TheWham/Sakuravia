"""Audio download and slicing helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..config import AppConfig
from ..models import VideoMetadata
from .process_runner import ProcessRunner


class SubtitleOrAudioService:
    """Handle the file-based side of the fallback branch when subtitles are missing."""

    def __init__(self, config: AppConfig, process_runner: ProcessRunner) -> None:
        self._config = config
        self._process_runner = process_runner

    def download_audio(self, metadata: VideoMetadata) -> Path:
        """Download the best available audio stream and keep it under the workspace."""
        video_dir = self._config.audio_dir / metadata.bvid
        run_dir = video_dir / f"run_{datetime.utcnow():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        output_template = run_dir / f"{metadata.bvid}.%(ext)s"
        ffmpeg_dir = Path(self._config.ffmpeg_bin).parent

        # Windows 上偶尔会有上次失败遗留且仍被锁住的 .part 文件。
        # 清理只是减少目录噪音，失败不能阻断本次下载，所以真正下载写入新的 run 目录。
        for stale_file in video_dir.glob(f"{metadata.bvid}.*.part"):
            try:
                stale_file.unlink(missing_ok=True)
            except OSError:
                continue

        self._process_runner.run(
            [
                self._config.yt_dlp_bin,
                "-x",
                "--audio-format",
                "m4a",
                "--ffmpeg-location",
                str(ffmpeg_dir),
                "--no-part",
                "--force-overwrites",
                "-o",
                str(output_template),
                metadata.webpage_url,
            ]
        )

        audio_files = sorted(run_dir.glob(f"{metadata.bvid}.*"))
        for audio_file in audio_files:
            if audio_file.suffix.lower() in {".m4a", ".mp3", ".wav", ".webm", ".aac", ".flac", ".ogg"}:
                return audio_file
        raise FileNotFoundError("yt-dlp 下载完成后未找到音频文件。")

    def split_audio(self, audio_path: Path, chunk_seconds: int = 600) -> list[Path]:
        """Split large audio files into fixed-size chunks accepted by the ASR provider."""
        target_dir = audio_path.parent / f"{audio_path.stem}_chunks"
        target_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = target_dir / "chunk_%03d.m4a"

        self._process_runner.run(
            [
                self._config.ffmpeg_bin,
                "-i",
                str(audio_path),
                "-f",
                "segment",
                "-segment_time",
                str(chunk_seconds),
                "-c",
                "copy",
                str(output_pattern),
            ]
        )

        chunks = sorted(target_dir.glob("chunk_*.m4a"))
        if not chunks:
            raise FileNotFoundError("ffmpeg 切片完成后未生成任何音频分片。")
        return chunks
