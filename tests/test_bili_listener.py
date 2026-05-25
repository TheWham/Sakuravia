"""Tests for Bilibili @ mention event handling."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.models import BiliDeliveryStatus, BiliEventStatus, MailStatus, TaskStatus
from app.services.bili_listener import BiliEventService, BiliListenerState, BiliMentionItem, BiliMentionPoller
from app.storage import TaskRepository


class BiliListenerConfigTests(unittest.TestCase):
    """Cover startup behavior when the listener is enabled but not configured."""

    def test_missing_cookie_keeps_listener_stopped(self) -> None:
        config = _config(Path(tempfile.gettempdir()), enable_listener=True)
        poller = BiliMentionPoller(config, FakeAuthService(), FakeMentionApi(), FakeEventService())

        poller.start()
        state = poller.snapshot()

        self.assertFalse(state.running)
        self.assertEqual(state.login_status, "CONFIG_ERROR")
        self.assertIn("BILI_COOKIE", state.last_error)


class BiliEventServiceTests(unittest.TestCase):
    """Verify @ mention events are persisted and converted into local tasks."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepository(Path(self.temp_dir.name) / "tasks.db")
        self.repo.init_db()
        self.task_service = FakeTaskService(self.repo)
        self.mail_service = FakeMailService()
        self.event_service = BiliEventService(self.repo, self.task_service, self.mail_service)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_valid_mention_creates_one_task(self) -> None:
        self.repo.upsert_bili_user_email("100", "测试用户", "target@example.com")
        mention = BiliMentionItem(
            notification_id="n1",
            comment_id="c1",
            sender_mid="100",
            sender_name="测试用户",
            content="@mysakura 请总结 BV1xx411c7mD",
            mentions_self=True,
        )

        self.event_service.process_mentions([mention])
        events = self.repo.list_bili_events()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, BiliEventStatus.TASK_CREATED)
        self.assertEqual(events[0].delivery_status, BiliDeliveryStatus.PENDING)
        self.assertEqual(events[0].recipient_email, "target@example.com")
        self.assertEqual(events[0].bvid, "BV1xx411c7mD")
        self.assertEqual(self.task_service.submit_count, 1)
        self.assertFalse(self.task_service.last_send_mail)

        self.event_service.process_mentions([mention])
        self.assertEqual(self.task_service.submit_count, 1)

    def test_mention_without_video_source_is_ignored(self) -> None:
        mention = BiliMentionItem(
            notification_id="n2",
            comment_id="c2",
            sender_mid="100",
            sender_name="测试用户",
            content="@mysakura 你好",
            mentions_self=True,
        )

        self.event_service.process_mentions([mention])
        event = self.repo.list_bili_events()[0]

        self.assertEqual(event.status, BiliEventStatus.IGNORED)
        self.assertIn("未包含", event.error_message)

    def test_mention_without_bound_email_fails_before_creating_task(self) -> None:
        mention = BiliMentionItem(
            notification_id="n3",
            comment_id="c3",
            sender_mid="404",
            sender_name="未绑定用户",
            content="@mysakura 请总结 BV1xx411c7mD",
            mentions_self=True,
        )

        self.event_service.process_mentions([mention])
        event = self.repo.list_bili_events()[0]

        self.assertEqual(event.status, BiliEventStatus.FAILED)
        self.assertEqual(event.delivery_status, BiliDeliveryStatus.FAILED)
        self.assertIn("未绑定邮箱", event.error_message)
        self.assertEqual(self.task_service.submit_count, 0)

    def test_success_task_reuse_sends_to_bound_user_email(self) -> None:
        self.repo.upsert_bili_user_email("100", "测试用户", "target@example.com")
        attachment = Path(self.temp_dir.name) / "done.md"
        attachment.write_text("# 已完成", encoding="utf-8")
        task = self.repo.create_task("BV1done", "BV1done")
        self.repo.update_task_fields(
            task.id,
            status=TaskStatus.SUCCESS.value,
            video_title="已完成视频",
            markdown_content="# 已完成",
            markdown_file_path=str(attachment),
        )
        self.task_service.reuse_task_id = task.id
        mention = BiliMentionItem(
            notification_id="n4",
            comment_id="c4",
            sender_mid="100",
            sender_name="测试用户",
            content="@mysakura 请总结 BV1done",
            mentions_self=True,
        )

        self.event_service.process_mentions([mention])
        event = self.repo.list_bili_events()[0]
        final_task = self.repo.get_task(task.id)

        self.assertEqual(event.status, BiliEventStatus.TASK_SUCCESS)
        self.assertEqual(event.delivery_status, BiliDeliveryStatus.SENT)
        self.assertRegex(event.delivered_at, re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"))
        self.assertEqual(final_task.mail_status, MailStatus.SENT)
        self.assertEqual(self.mail_service.sent_to, ["target@example.com"])


class FakeTaskService:
    """Small task service double that creates pending rows through the real repository."""

    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository
        self.submit_count = 0
        self.reuse_task_id: int | None = None
        self.last_send_mail: bool | None = None

    def submit_task(self, source_input: str, reuse_success: bool = False, send_mail: bool = True):
        self.submit_count += 1
        self.last_send_mail = send_mail
        if self.reuse_task_id is not None:
            return self.repository.get_task(self.reuse_task_id)
        return self.repository.create_task(source_input, source_input, send_mail=send_mail)

    def get_task(self, task_id: int):
        return self.repository.get_task(task_id)


class FakeAuthService:
    """Unused by config-error test, but required by the poller constructor."""

    def check_login(self) -> dict[str, str]:
        return {"mid": "1", "uname": "tester"}


class FakeMentionApi:
    """Unused by config-error test, but required by the poller constructor."""

    def fetch_mentions(self) -> list[BiliMentionItem]:
        return []


class FakeEventService:
    """Unused by config-error test, but required by the poller constructor."""

    def process_mentions(self, mentions: list[BiliMentionItem]) -> None:
        return None


class FakeMailService:
    """Capture target recipients for B 站 event delivery tests."""

    def __init__(self) -> None:
        self.sent_to: list[str] = []

    def send_markdown(self, subject_title: str, markdown_content: str, attachment_path: Path, to_email: str | None = None) -> None:
        self.sent_to.append(to_email or "")


def _config(root: Path, enable_listener: bool = False) -> AppConfig:
    """Build the minimal config needed by Bili listener tests."""
    return AppConfig(
        app_host="127.0.0.1",
        app_port=8000,
        sqlite_path=root / "tasks.db",
        output_dir=root / "output",
        audio_dir=root / "audio",
        tmp_dir=root / "tmp",
        yt_dlp_bin="yt-dlp",
        ffmpeg_bin="ffmpeg",
        groq_api_key="",
        groq_asr_model="whisper-large-v3-turbo",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_api_key="",
        deepseek_model="deepseek-chat",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="",
        smtp_password="",
        smtp_use_ssl=True,
        mail_from="",
        mail_to="",
        bili_enable_listener=enable_listener,
    )
