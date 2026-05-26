"""Tests for Bilibili @ mention event handling."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.models import BiliDeliveryStatus, BiliEventStatus, MailStatus, TaskStatus
from app.services.bili_listener import BiliApiError, BiliEventService, BiliMentionApi, BiliMentionItem, BiliMentionPoller
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

    def test_mention_api_parses_at_time_as_timestamp(self) -> None:
        """The @ feed timestamp is required for startup-time filtering."""
        api = BiliMentionApi(FakeHttpClient([
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "id": "n-at-time",
                            "at_time": 1001,
                            "user": {"mid": "100", "nickname": "测试用户"},
                            "item": {
                                "target_id": "c1",
                                "source_content": "@mysakura BV1time11111",
                            },
                        }
                    ]
                },
            }
        ]), "1")

        mentions = api.fetch_mentions()

        self.assertEqual(mentions[0].notification_id, "n-at-time")
        self.assertEqual(mentions[0].timestamp_seconds, 1001)

    def test_mention_api_normalizes_millisecond_timestamp(self) -> None:
        """Some loose API fields may use millisecond timestamps."""
        api = BiliMentionApi(FakeHttpClient([
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "id": "n-ms",
                            "user": {"mid": "100", "nickname": "测试用户"},
                            "item": {
                                "target_id": "c1",
                                "ctime": 1_001_000_000_000,
                                "source_content": "@mysakura BV1time22222",
                            },
                        }
                    ]
                },
            }
        ]), "1")

        mentions = api.fetch_mentions()

        self.assertEqual(mentions[0].timestamp_seconds, 1_001_000_000)

    def test_poll_once_checks_login_on_first_and_tenth_poll_only(self) -> None:
        """Normal polling should not hit the account nav endpoint on every loop."""
        auth_service = FakeAuthService()
        mention_api = FakeMentionApi()
        poller = BiliMentionPoller(
            _config(Path(tempfile.gettempdir()), enable_listener=True, with_credentials=True),
            auth_service,
            mention_api,
            FakeEventService(),
        )

        for _ in range(9):
            poller.poll_once()
        self.assertEqual(auth_service.check_count, 1)
        self.assertEqual(mention_api.fetch_count, 9)

        poller.poll_once()
        self.assertEqual(auth_service.check_count, 2)
        self.assertEqual(mention_api.fetch_count, 10)

    def test_poll_processes_only_mentions_after_startup_time(self) -> None:
        """Old @ messages can reappear in the feed, but should not be processed."""
        mention_api = FakeMentionApi(
            batches=[
                [
                    _mention("old-1", "BV1old111111", timestamp_seconds=900),
                    _mention("new-1", "BV1new111111", timestamp_seconds=1001),
                ],
            ]
        )
        event_service = FakeEventService()
        poller = BiliMentionPoller(
            _config(Path(tempfile.gettempdir()), enable_listener=True, with_credentials=True),
            FakeAuthService(),
            mention_api,
            event_service,
        )
        poller._startup_cutoff_seconds = 1000
        poller.snapshot().startup_cutoff_seconds = 1000

        poller.poll_once()
        state = poller.snapshot()

        self.assertEqual(state.skipped_old_count, 1)
        self.assertEqual(
            [[mention.notification_id for mention in batch] for batch in event_service.processed_batches],
            [["new-1"]],
        )

    def test_timestamp_filter_deduplicates_new_mentions_across_polls(self) -> None:
        """A new notification should only reach event handling once per process."""
        mention_api = FakeMentionApi(
            batches=[
                [_mention("new-1", "BV1new111111", timestamp_seconds=1001)],
                [
                    _mention("new-1", "BV1new111111", timestamp_seconds=1001),
                    _mention("new-2", "BV1new222222", timestamp_seconds=1002),
                ],
            ]
        )
        event_service = FakeEventService()
        poller = BiliMentionPoller(
            _config(Path(tempfile.gettempdir()), enable_listener=True, with_credentials=True),
            FakeAuthService(),
            mention_api,
            event_service,
        )
        poller._startup_cutoff_seconds = 1000
        poller.snapshot().startup_cutoff_seconds = 1000

        poller.poll_once()
        poller.poll_once()

        self.assertEqual(
            [[mention.notification_id for mention in batch] for batch in event_service.processed_batches],
            [["new-1"], ["new-2"]],
        )

    def test_mentions_without_timestamp_are_skipped(self) -> None:
        """A missing B 站 timestamp is treated as unsafe to avoid old-message resends."""
        mention_api = FakeMentionApi(batches=[[_mention("missing-time", "BV1miss11111")]])
        event_service = FakeEventService()
        poller = BiliMentionPoller(
            _config(Path(tempfile.gettempdir()), enable_listener=True, with_credentials=True),
            FakeAuthService(),
            mention_api,
            event_service,
        )
        poller._startup_cutoff_seconds = 1000
        poller.snapshot().startup_cutoff_seconds = 1000

        poller.poll_once()

        self.assertEqual(poller.snapshot().skipped_old_count, 1)
        self.assertEqual(event_service.processed_batches, [])

    def test_schedule_next_poll_uses_configured_random_range(self) -> None:
        """The visible next-poll interval should stay inside the configured range."""
        config = _config(Path(tempfile.gettempdir()), enable_listener=True, with_credentials=True, poll_min=180, poll_max=480)
        poller = BiliMentionPoller(config, FakeAuthService(), FakeMentionApi(), FakeEventService())

        for _ in range(20):
            wait_seconds = poller._schedule_next_poll()
            self.assertGreaterEqual(wait_seconds, 180)
            self.assertLessEqual(wait_seconds, 480)
            self.assertEqual(poller.snapshot().last_interval_seconds, wait_seconds)
            self.assertTrue(poller.snapshot().next_poll_at)

    def test_retryable_failure_enters_first_backoff_range(self) -> None:
        """Temporary failures should back off instead of pausing immediately."""
        mention_api = FakeMentionApi(errors=[RuntimeError("temporary network error")])
        poller = BiliMentionPoller(
            _config(Path(tempfile.gettempdir()), enable_listener=True, with_credentials=True),
            FakeAuthService(),
            mention_api,
            FakeEventService(),
        )

        poller.poll_once()
        state = poller.snapshot()

        self.assertEqual(state.login_status, "ERROR")
        self.assertEqual(state.consecutive_failures, 1)
        self.assertFalse(state.paused_reason)

        wait_seconds = poller._schedule_next_poll()
        self.assertGreaterEqual(wait_seconds, 300)
        self.assertLessEqual(wait_seconds, 600)

    def test_risk_control_error_pauses_listener(self) -> None:
        """412/429/captcha-style responses should stop future B 站 requests."""
        mention_api = FakeMentionApi(errors=[BiliApiError("HTTP Error 412: Precondition Failed")])
        poller = BiliMentionPoller(
            _config(Path(tempfile.gettempdir()), enable_listener=True, with_credentials=True),
            FakeAuthService(),
            mention_api,
            FakeEventService(),
        )

        poller.poll_once()
        state = poller.snapshot()

        self.assertEqual(state.login_status, "PAUSED")
        self.assertIn("疑似风控", state.paused_reason)
        self.assertIn("412", state.last_error)

        poller.poll_once()
        self.assertEqual(mention_api.fetch_count, 1)

    def test_run_loop_stops_after_pause(self) -> None:
        """A paused listener should leave the background loop without sleeping forever."""
        mention_api = FakeMentionApi(errors=[BiliApiError("B 站接口返回错误：429 Too Many Requests")])
        poller = BiliMentionPoller(
            _config(Path(tempfile.gettempdir()), enable_listener=True, with_credentials=True),
            FakeAuthService(),
            mention_api,
            FakeEventService(),
        )

        poller._run_loop()

        self.assertFalse(poller.snapshot().running)
        self.assertEqual(mention_api.fetch_count, 1)
        self.assertTrue(poller.snapshot().paused_reason)


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
    """Small auth double that can count or script login checks."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.errors = errors or []
        self.check_count = 0

    def check_login(self) -> dict[str, str]:
        self.check_count += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"mid": "1", "uname": "tester"}


class FakeHttpClient:
    """Return scripted B 站 API payloads for parser-level tests."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses

    def get_json(self, url: str, params: dict[str, object] | None = None) -> dict[str, object]:
        return self.responses.pop(0)


class FakeMentionApi:
    """Small @ feed double that can count or script fetches."""

    def __init__(
        self,
        mentions: list[BiliMentionItem] | None = None,
        errors: list[Exception] | None = None,
        batches: list[list[BiliMentionItem]] | None = None,
    ) -> None:
        self.mentions = mentions or []
        self.errors = errors or []
        self.batches = batches or []
        self.fetch_count = 0

    def fetch_mentions(self) -> list[BiliMentionItem]:
        self.fetch_count += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.batches:
            return self.batches.pop(0)
        return self.mentions


class FakeEventService:
    """Unused by config-error test, but required by the poller constructor."""

    def __init__(self) -> None:
        self.processed_batches: list[list[BiliMentionItem]] = []
        self.call_count = 0

    def process_mentions(self, mentions: list[BiliMentionItem]) -> None:
        self.call_count += 1
        if mentions:
            self.processed_batches.append(mentions)


class FakeMailService:
    """Capture target recipients for B 站 event delivery tests."""

    def __init__(self) -> None:
        self.sent_to: list[str] = []

    def send_markdown(self, subject_title: str, markdown_content: str, attachment_path: Path, to_email: str | None = None) -> None:
        self.sent_to.append(to_email or "")


def _config(
    root: Path,
    enable_listener: bool = False,
    with_credentials: bool = False,
    poll_min: int = 180,
    poll_max: int = 480,
) -> AppConfig:
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
        bili_cookie="SESSDATA=test" if with_credentials else "",
        bili_self_mid="1" if with_credentials else "",
        bili_poll_min_seconds=poll_min,
        bili_poll_max_seconds=poll_max,
        bili_poll_interval_seconds=poll_min,
    )


def _mention(notification_id: str, bvid: str, timestamp_seconds: int = 0) -> BiliMentionItem:
    """Build a valid mention item for poller-level tests."""
    return BiliMentionItem(
        notification_id=notification_id,
        comment_id=f"comment-{notification_id}",
        sender_mid="100",
        sender_name="测试用户",
        content=f"@mysakura 请总结 {bvid}",
        mentions_self=True,
        timestamp_seconds=timestamp_seconds,
    )
