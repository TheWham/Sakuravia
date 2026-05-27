"""Audio download and slicing helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..config import AppConfig
from ..models import VideoMetadata
from .process_runner import ProcessRunner


AUDIO_DOWNLOAD_TIMEOUT_SECONDS = 3600
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 3600
AUDIO_SPLIT_TIMEOUT_SECONDS = 1200
AUDIO_NORMALIZE_TIMEOUT_SECONDS = 1200
VIDEO_NORMALIZE_TIMEOUT_SECONDS = 3600
MIMO_AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".wav", ".ogg"}
DOWNLOAD_AUDIO_SUFFIXES = {*MIMO_AUDIO_SUFFIXES, ".webm", ".aac"}


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
                "mp3",
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
            if audio_file.suffix.lower() == ".mp3":
                return audio_file
        for audio_file in audio_files:
            if audio_file.suffix.lower() in DOWNLOAD_AUDIO_SUFFIXES:
                return self._normalize_audio_for_mimo(audio_file)
        raise FileNotFoundError("yt-dlp 下载完成后未找到音频文件。")

    def download_video(self, metadata: VideoMetadata) -> Path:
        """Download a single playable video file for Mimo video understanding.

        视频理解只作为 V4 的兜底或手动增强路径。这里仍按每次任务单独 run 目录
        保存，避免上一次失败留下的临时文件影响本次判断和 OSS 上传。
        """
        video_dir = self._config.audio_dir / metadata.bvid
        run_dir = video_dir / f"video_{datetime.utcnow():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        output_template = run_dir / f"{metadata.bvid}.%(ext)s"

        self._process_runner.run(
            self._build_yt_dlp_args(
                "--no-playlist",
                "-f",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                *self._build_ffmpeg_location_args(),
                "--no-part",
                "--force-overwrites",
                "-o",
                str(output_template),
                metadata.webpage_url,
            ),
            timeout_seconds=VIDEO_DOWNLOAD_TIMEOUT_SECONDS,
        )

        video_files = sorted(run_dir.glob(f"{metadata.bvid}.*"))
        for video_file in video_files:
            if video_file.suffix.lower() in {".mp4", ".mov", ".avi", ".wmv"}:
                return self._normalize_video_for_mimo(video_file)
        raise FileNotFoundError("yt-dlp 下载完成后未找到视频文件。")

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

    def _normalize_audio_for_mimo(self, audio_path: Path) -> Path:
        """Convert yt-dlp fallback formats into a conservative MP3 for Mimo.

        Mimo 的音频 URL 方式只接受 MP3、WAV、FLAC、M4A、OGG。B 站偶尔会让
        yt-dlp 留下 m4a/webm/aac 等中间结果。官方也提示格式变体不保证都识别，
        所以这里统一转成兼容性更稳的 MP3，避免把供应商格式错误暴露到最后一步。
        """
        target_path = audio_path.with_suffix(".mp3")
        if target_path == audio_path:
            return audio_path
        self._process_runner.run(
            [
                self._config.ffmpeg_bin,
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(target_path),
            ],
            timeout_seconds=AUDIO_NORMALIZE_TIMEOUT_SECONDS,
        )
        if not target_path.exists() or target_path.stat().st_size <= 0:
            raise FileNotFoundError("ffmpeg 音频格式转换完成后未生成 mp3 文件。")
        return target_path

    def _normalize_video_for_mimo(self, video_path: Path) -> Path:
        """Transcode B 站 video variants into a conservative Mimo-friendly MP4.

        官方只承诺 MP4/MOV/AVI/WMV 这类容器，且明确不同格式变体不保证都可识别。
        B 站视频常见 AV1、HEVC、分离音轨等组合，虽然文件后缀是 mp4，但多模态
        网关仍可能报 corrupted。这里统一转成 H.264 + AAC + yuv420p，兼容性更稳。
        """
        target_path = video_path.with_name(f"{video_path.stem}_mimo.mp4")
        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path
        self._process_runner.run(
            [
                self._config.ffmpeg_bin,
                "-y",
                "-i",
                str(video_path),
                "-vf",
                "scale='min(1280,iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(target_path),
            ],
            timeout_seconds=VIDEO_NORMALIZE_TIMEOUT_SECONDS,
        )
        if not target_path.exists() or target_path.stat().st_size <= 0:
            raise FileNotFoundError("ffmpeg 视频格式转换完成后未生成 Mimo 兼容 mp4 文件。")
        return target_path

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
