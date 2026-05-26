"""Audio download and slicing helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..config import AppConfig
from ..models import VideoMetadata
from .process_runner import ProcessRunner


AUDIO_DOWNLOAD_TIMEOUT_SECONDS = 3600
AUDIO_SPLIT_TIMEOUT_SECONDS = 1200


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
        # Windows 上偶尔会有上次失败遗留且仍被锁住的 .part 文件。
        # 清理只是减少目录噪音，失败不能阻断本次下载，所以真正下载写入新的 run 目录。
        for stale_file in video_dir.glob(f"{metadata.bvid}.*.part"):
            try:
                stale_file.unlink(missing_ok=True)
            except OSError:
                continue

        self._process_runner.run(
            self._build_yt_dlp_args(
                "-x",
                "--no-playlist",
                "--audio-format",
                "m4a",
                *self._build_ffmpeg_location_args(),
                "--no-part",
                "--force-overwrites",
                "-o",
                str(output_template),
                metadata.webpage_url,
            ),
            timeout_seconds=AUDIO_DOWNLOAD_TIMEOUT_SECONDS,
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
            ],
            timeout_seconds=AUDIO_SPLIT_TIMEOUT_SECONDS,
        )

        chunks = sorted(target_dir.glob("chunk_*.m4a"))
        if not chunks:
            raise FileNotFoundError("ffmpeg 切片完成后未生成任何音频分片。")
        return chunks

    def _build_yt_dlp_args(self, *args: str) -> list[str]:
        """Build yt-dlp arguments with an optional cookies.txt login state.

        服务器环境下 B 站经常对无登录态的机房 IP 返回 412；这里统一给下载
        分支带上 cookies 文件，避免解析能过但真正下载音频时再次被拦。
        """
        command = [self._config.yt_dlp_bin]
        if self._config.yt_dlp_cookies_file is not None:
            command.extend(["--cookies", str(self._config.yt_dlp_cookies_file)])
        command.extend(args)
        return command

    def _build_ffmpeg_location_args(self) -> list[str]:
        """Return yt-dlp's ffmpeg location only when we have a concrete path.

        `FFMPEG_BIN=ffmpeg` means "use PATH". Passing its parent directory to
        yt-dlp would become `--ffmpeg-location .`, which breaks Ubuntu servers.
        Absolute paths such as `/usr/bin/ffmpeg` or Windows install paths still
        provide the directory so yt-dlp can find both ffmpeg and ffprobe.
        """
        ffmpeg_path = Path(self._config.ffmpeg_bin)
        if ffmpeg_path.parent == Path("."):
            return []
        return ["--ffmpeg-location", str(ffmpeg_path.parent)]
