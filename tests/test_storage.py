"""Repository and task orchestration tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.models import MailStatus, SummaryResult, TaskStatus, TranscriptResult, VideoMetadata
from app.storage import TaskRepository
from app.services.task_service import TaskService


class RepositoryTests(unittest.TestCase):
    """Cover status persistence and active-task deduplication queries."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tasks.db"
        self.repo = TaskRepository(self.db_path)
        self.repo.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_and_find_active_task(self) -> None:
        task = self.repo.create_task("BV1abc", "BV1abc")
        active = self.repo.find_active_task_by_bvid("BV1abc")
        self.assertIsNotNone(active)
        self.assertEqual(active.id, task.id)

    def test_finished_task_is_not_reported_as_active(self) -> None:
        task = self.repo.create_task("BV1abc", "BV1abc")
        self.repo.update_task_fields(task.id, status=TaskStatus.SUCCESS.value)
        active = self.repo.find_active_task_by_bvid("BV1abc")
        self.assertIsNone(active)


class TaskServiceTests(unittest.TestCase):
    """Exercise the main happy path with lightweight service doubles."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tasks.db"
        self.repo = TaskRepository(self.db_path)
        self.repo.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_process_task_updates_final_state(self) -> None:
        task = self.repo.create_task("BV1test", "BV1test")
        service = TaskService(
            repository=self.repo,
            bili_service=FakeBiliService(),
            subtitle_audio_service=FakeAudioService(Path(self.temp_dir.name)),
            transcription_service=FakeTranscriptionService(),
            summary_service=FakeSummaryService(),
            artifact_service=FakeArtifactService(Path(self.temp_dir.name)),
            mail_service=FakeMailService(),
        )

        service._process_task(task.id)
        final_task = self.repo.get_task(task.id)

        self.assertEqual(final_task.status, TaskStatus.SUCCESS)
        self.assertEqual(final_task.subtitle_source, "official_subtitle")
        self.assertEqual(final_task.mail_status, MailStatus.SENT)
        self.assertIn("# 视频标题", final_task.markdown_content)
        self.assertTrue(final_task.markdown_file_path.endswith(".md"))


class FakeBiliService:
    """Simple stub that keeps the task flow deterministic in tests."""

    def normalize_source(self, source: str) -> str:
        return source

    def fetch_metadata(self, bvid: str) -> VideoMetadata:
        return VideoMetadata(
            bvid=bvid,
            title="测试视频",
            uploader="测试UP",
            duration=120,
            webpage_url=f"https://www.bilibili.com/video/{bvid}",
        )

    def fetch_metadata_for_source(self, bvid: str, source_input: str) -> VideoMetadata:
        return self.fetch_metadata(bvid)

    def fetch_subtitles(self, metadata: VideoMetadata) -> TranscriptResult:
        return TranscriptResult(source="official_subtitle", full_text="第一段\n第二段")


class FakeAudioService:
    """The happy path does not use audio download, but the dependency must exist."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def download_audio(self, metadata: VideoMetadata) -> Path:
        file_path = self.root / "sample.m4a"
        file_path.write_bytes(b"test")
        return file_path

    def split_audio(self, audio_path: Path, chunk_seconds: int = 600) -> list[Path]:
        return [audio_path]


class FakeTranscriptionService:
    """Used only when the subtitle branch is unavailable."""

    def transcribe_audio(self, audio_path: Path) -> TranscriptResult:
        return TranscriptResult(source="asr_audio", full_text="转写内容")


class FakeSummaryService:
    """Return stable Markdown so the final assertions stay simple."""

    def summarize(self, metadata: VideoMetadata, transcript: TranscriptResult) -> SummaryResult:
        markdown = "\n".join(
            [
                "# 视频标题",
                "## 视频概览",
                "测试概览",
                "## 核心观点",
                "- 要点一",
                "## 分段摘要",
                transcript.full_text,
                "## 关键信息",
                "- 信息一",
                "## 可执行结论",
                "- 结论一",
            ]
        )
        return SummaryResult(markdown=markdown, title=metadata.title, highlights=["要点一"])


class FakeArtifactService:
    """Write a small markdown file to the temporary directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write_markdown(self, bvid: str, title: str, markdown: str) -> Path:
        file_path = self.root / f"{bvid}.md"
        file_path.write_text(markdown, encoding="utf-8")
        return file_path


class FakeMailService:
    """Capture the fact that mail would have been sent."""

    def send_markdown(self, subject_title: str, markdown_content: str, attachment_path: Path) -> None:
        return None
