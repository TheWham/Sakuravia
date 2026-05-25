"""Repository and task orchestration tests."""

from __future__ import annotations

import re
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

    def test_interrupted_tasks_are_failed_on_startup_recovery(self) -> None:
        active = self.repo.create_task("BV1active", "BV1active")
        done = self.repo.create_task("BV1done", "BV1done")
        self.repo.update_task_fields(active.id, status=TaskStatus.DOWNLOADING_AUDIO.value)
        self.repo.update_task_fields(done.id, status=TaskStatus.SUCCESS.value, mail_status=MailStatus.SENT.value)

        interrupted = self.repo.fail_interrupted_tasks()
        active_after = self.repo.get_task(active.id)
        done_after = self.repo.get_task(done.id)

        self.assertEqual([task.id for task in interrupted], [active.id])
        self.assertEqual(active_after.status, TaskStatus.FAILED)
        self.assertEqual(active_after.mail_status, MailStatus.FAILED)
        self.assertIn("重新提交", active_after.error_message)
        self.assertEqual(done_after.status, TaskStatus.SUCCESS)

    def test_interrupted_tasks_are_prepared_for_retry(self) -> None:
        active = self.repo.create_task("BV1active", "BV1active")
        done = self.repo.create_task("BV1done", "BV1done")
        self.repo.update_task_fields(active.id, status=TaskStatus.DOWNLOADING_AUDIO.value)
        self.repo.update_task_fields(done.id, status=TaskStatus.SUCCESS.value, mail_status=MailStatus.SENT.value)

        retryable = self.repo.prepare_interrupted_tasks_for_retry()
        active_after = self.repo.get_task(active.id)
        done_after = self.repo.get_task(done.id)

        self.assertEqual([task.id for task in retryable], [active.id])
        self.assertEqual(active_after.status, TaskStatus.PENDING)
        self.assertEqual(active_after.mail_status, MailStatus.PENDING)
        self.assertEqual(active_after.retry_count, 1)
        self.assertEqual(active_after.last_checkpoint, "startup_retry")
        self.assertEqual(done_after.status, TaskStatus.SUCCESS)

    def test_failed_bili_events_reopen_when_task_is_retried(self) -> None:
        task = self.repo.create_task("BV1event", "BV1event")
        event = self.repo.create_bili_event_if_absent("n1", "c1", "100", "测试用户", "@ai BV1event")
        self.repo.update_bili_event_fields(
            event.id,
            task_id=task.id,
            bvid="BV1event",
            status="TASK_FAILED",
            delivery_status="FAILED",
            error_message="old failure",
        )

        reopened = self.repo.reopen_bili_events_for_task(task.id)
        final_event = self.repo.get_bili_event_by_id(event.id)

        self.assertEqual([event.id for event in reopened], [event.id])
        self.assertEqual(final_event.status.value, "TASK_CREATED")
        self.assertEqual(final_event.delivery_status.value, "PENDING")
        self.assertEqual(final_event.error_message, "")

    def test_success_task_can_be_reused_for_bili_listener(self) -> None:
        task = self.repo.create_task("BV1abc", "BV1abc")
        self.repo.update_task_fields(task.id, status=TaskStatus.SUCCESS.value)

        reusable = self.repo.find_reusable_task_by_bvid("BV1abc", "BV1abc", include_success=True)

        self.assertIsNotNone(reusable)
        self.assertEqual(reusable.id, task.id)

    def test_create_task_can_disable_default_mail(self) -> None:
        task = self.repo.create_task("BV1abc", "BV1abc", send_mail=False)

        self.assertFalse(task.send_mail)

    def test_created_time_uses_shanghai_readable_format(self) -> None:
        task = self.repo.create_task("BV1abc", "BV1abc")

        self.assertNotIn("Z", task.created_at)
        self.assertNotIn("T", task.created_at)
        self.assertRegex(task.created_at, re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"))
        self.assertRegex(task.updated_at, re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"))

    def test_bili_user_email_can_be_upserted_and_deleted(self) -> None:
        created = self.repo.upsert_bili_user_email("100", "旧名字", "old@example.com")
        updated = self.repo.upsert_bili_user_email("100", "新名字", "new@example.com")

        self.assertEqual(created.uid, "100")
        self.assertEqual(updated.id, created.id)
        self.assertEqual(updated.username, "新名字")
        self.assertEqual(updated.email, "new@example.com")
        self.assertEqual(len(self.repo.list_bili_user_emails()), 1)

        self.repo.delete_bili_user_email("100")
        self.assertIsNone(self.repo.find_bili_user_email_by_uid("100"))


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
        mail_service = FakeMailService()
        service = TaskService(
            repository=self.repo,
            bili_service=FakeBiliService(),
            subtitle_audio_service=FakeAudioService(Path(self.temp_dir.name)),
            transcription_service=FakeTranscriptionService(),
            summary_service=FakeSummaryService(),
            artifact_service=FakeArtifactService(Path(self.temp_dir.name)),
            mail_service=mail_service,
        )

        service._process_task(task.id)
        final_task = self.repo.get_task(task.id)

        self.assertEqual(final_task.status, TaskStatus.SUCCESS)
        self.assertEqual(final_task.subtitle_source, "official_subtitle")
        self.assertEqual(final_task.mail_status, MailStatus.SENT)
        self.assertEqual(mail_service.send_count, 1)
        self.assertIn("# 视频标题", final_task.markdown_content)
        self.assertTrue(final_task.markdown_file_path.endswith(".md"))

    def test_process_task_can_skip_default_mail(self) -> None:
        task = self.repo.create_task("BV1test", "BV1test", send_mail=False)
        mail_service = FakeMailService()
        service = TaskService(
            repository=self.repo,
            bili_service=FakeBiliService(),
            subtitle_audio_service=FakeAudioService(Path(self.temp_dir.name)),
            transcription_service=FakeTranscriptionService(),
            summary_service=FakeSummaryService(),
            artifact_service=FakeArtifactService(Path(self.temp_dir.name)),
            mail_service=mail_service,
        )

        service._process_task(task.id)
        final_task = self.repo.get_task(task.id)

        self.assertEqual(final_task.status, TaskStatus.SUCCESS)
        self.assertEqual(final_task.mail_status, MailStatus.SKIPPED)
        self.assertEqual(mail_service.send_count, 0)

    def test_retry_uses_existing_markdown_and_only_resends_mail(self) -> None:
        markdown_path = Path(self.temp_dir.name) / "done.md"
        markdown_path.write_text("# 已完成", encoding="utf-8")
        task = self.repo.create_task("BV1test", "BV1test")
        self.repo.update_task_fields(
            task.id,
            status=TaskStatus.FAILED.value,
            video_title="已有结果",
            markdown_file_path=str(markdown_path),
            markdown_content="# 已完成",
            mail_status=MailStatus.PENDING.value,
        )
        mail_service = FakeMailService()
        bili_service = FakeBiliService()
        service = TaskService(
            repository=self.repo,
            bili_service=bili_service,
            subtitle_audio_service=FakeAudioService(Path(self.temp_dir.name)),
            transcription_service=FakeTranscriptionService(),
            summary_service=FakeSummaryService(),
            artifact_service=FakeArtifactService(Path(self.temp_dir.name)),
            mail_service=mail_service,
        )

        service._process_task(task.id)
        final_task = self.repo.get_task(task.id)

        self.assertEqual(final_task.status, TaskStatus.SUCCESS)
        self.assertEqual(final_task.mail_status, MailStatus.SENT)
        self.assertEqual(final_task.last_checkpoint, "mail_sent")
        self.assertEqual(mail_service.send_count, 1)
        self.assertEqual(bili_service.fetch_metadata_count, 0)


class FakeBiliService:
    """Simple stub that keeps the task flow deterministic in tests."""

    def __init__(self) -> None:
        self.fetch_metadata_count = 0

    def normalize_source(self, source: str) -> str:
        return source

    def fetch_metadata(self, bvid: str) -> VideoMetadata:
        self.fetch_metadata_count += 1
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

    def __init__(self) -> None:
        self.send_count = 0

    def send_markdown(
        self,
        subject_title: str,
        markdown_content: str,
        attachment_path: Path,
        to_email: str | None = None,
    ) -> None:
        self.send_count += 1
        return None
