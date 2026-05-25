"""Bilibili mention listener based on the web notification API."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError
from pathlib import Path

from ..config import AppConfig
from ..models import (
    BiliDeliveryStatus,
    BiliEventRecord,
    BiliEventStatus,
    MailStatus,
    TaskRecord,
    TaskStatus,
    utc_now_text,
)
from ..storage import TaskRepository
from ..utils import extract_bilibili_video_source
from .mail import MailService
from .task_service import TaskService


class BiliApiError(RuntimeError):
    """Raised when a Bilibili web API call cannot be used safely."""


@dataclass(slots=True)
class BiliMentionItem:
    """One parsed @ notification item from Bilibili."""

    notification_id: str
    comment_id: str
    sender_mid: str
    sender_name: str
    content: str
    mentions_self: bool


@dataclass(slots=True)
class BiliListenerState:
    """Runtime state exposed to the local page."""

    enabled: bool
    running: bool
    login_status: str
    account_mid: str
    account_name: str
    last_poll_at: str
    last_error: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly snapshot."""
        return asdict(self)


class BiliHttpClient:
    """Small Cookie-authenticated client for Bilibili web APIs."""

    _USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

    def __init__(self, config: AppConfig) -> None:
        self._cookie = config.bili_cookie
        self._timeout = config.bili_request_timeout_seconds
        # B 站接口面向国内网络，直接访问通常更稳定。这里显式关闭 urllib
        # 对 HTTP_PROXY/HTTPS_PROXY 的自动读取，避免本机代理端口未启动时轮询失败。
        self._opener = request.build_opener(request.ProxyHandler({}))

    def get_json(self, url: str, params: dict[str, object] | None = None) -> dict[str, object]:
        """Send one GET request and validate the standard Bilibili JSON envelope."""
        query = parse.urlencode(params or {}, doseq=True)
        target_url = f"{url}?{query}" if query else url
        req = request.Request(target_url, method="GET")
        req.add_header("User-Agent", self._USER_AGENT)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Referer", "https://www.bilibili.com/")
        if self._cookie:
            req.add_header("Cookie", self._cookie)

        try:
            with self._opener.open(req, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BiliApiError(f"B 站接口请求失败：{exc}") from exc

        code = payload.get("code", 0)
        if code not in (0, "0"):
            message = payload.get("message") or payload.get("msg") or "未知错误"
            raise BiliApiError(f"B 站接口返回错误：{code} {message}")
        return payload


class BiliAuthService:
    """Check whether the configured Cookie still belongs to a logged-in Bilibili account."""

    _NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

    def __init__(self, http_client: BiliHttpClient) -> None:
        self._http_client = http_client

    def check_login(self) -> dict[str, str]:
        """Return basic account information or raise a clear auth error."""
        payload = self._http_client.get_json(self._NAV_URL)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data.get("isLogin"):
            raise BiliApiError("B 站 Cookie 未登录或已失效。")
        return {
            "mid": str(data.get("mid", "")),
            "uname": str(data.get("uname", "")),
        }


class BiliMentionApi:
    """Fetch @ mention notifications from Bilibili."""

    _AT_URL = "https://api.bilibili.com/x/msgfeed/at"

    def __init__(self, http_client: BiliHttpClient, self_mid: str) -> None:
        self._http_client = http_client
        self._self_mid = self_mid

    def fetch_mentions(self) -> list[BiliMentionItem]:
        """Load recent @ notifications and normalize their nested JSON fields."""
        payload = self._http_client.get_json(self._AT_URL, {"platform": "web"})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw_items = data.get("items") if isinstance(data.get("items"), list) else []
        return [self._parse_item(item) for item in raw_items if isinstance(item, dict)]

    def _parse_item(self, item: dict[str, Any]) -> BiliMentionItem:
        """Extract stable fields from Bilibili's loosely documented notification item."""
        nested_item = item.get("item") if isinstance(item.get("item"), dict) else {}
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        notification_id = str(item.get("id") or nested_item.get("id") or nested_item.get("notify_id") or "")
        comment_id = str(nested_item.get("target_id") or nested_item.get("source_id") or nested_item.get("rpid") or "")
        sender_mid = str(user.get("mid") or nested_item.get("sender_uid") or nested_item.get("uid") or "")
        sender_name = str(user.get("nickname") or user.get("uname") or nested_item.get("sender_uname") or "")
        content = str(
            nested_item.get("source_content")
            or nested_item.get("title")
            or nested_item.get("content")
            or item.get("title")
            or ""
        )
        mentions_self = self._mentions_self(nested_item.get("at_details"))
        if not notification_id:
            notification_id = f"{comment_id}:{sender_mid}:{hash(content)}"
        return BiliMentionItem(
            notification_id=notification_id,
            comment_id=comment_id,
            sender_mid=sender_mid,
            sender_name=sender_name,
            content=content,
            mentions_self=mentions_self,
        )

    def _mentions_self(self, at_details: object) -> bool:
        """Check at_details when Bilibili includes it; otherwise trust the @ feed."""
        if not isinstance(at_details, list) or not at_details:
            return True
        if not self._self_mid:
            return False
        for item in at_details:
            if isinstance(item, dict) and str(item.get("mid") or item.get("uid") or "") == self._self_mid:
                return True
        return False


class BiliEventService:
    """Turn Bilibili mention events into local summary tasks."""

    def __init__(self, repository: TaskRepository, task_service: TaskService, mail_service: MailService) -> None:
        self._repository = repository
        self._task_service = task_service
        self._mail_service = mail_service

    def process_mentions(self, mentions: list[BiliMentionItem]) -> None:
        """Persist every mention once and create tasks for valid commands."""
        for mention in mentions:
            event = self._repository.create_bili_event_if_absent(
                notification_id=mention.notification_id,
                comment_id=mention.comment_id,
                sender_mid=mention.sender_mid,
                sender_name=mention.sender_name,
                content=mention.content,
            )
            if event.status is not BiliEventStatus.NEW:
                continue
            self._process_one_event(event, mention)
        self.sync_task_statuses()

    def sync_task_statuses(self) -> None:
        """Propagate linked task terminal states back to B 站 events."""
        for event in self._repository.list_open_bili_events():
            if event.task_id is None:
                continue
            try:
                task = self._task_service.get_task(event.task_id)
            except KeyError:
                self._repository.update_bili_event_fields(
                    event.id,
                    status=BiliEventStatus.FAILED.value,
                    error_message="关联任务不存在。",
                )
                continue
            if task.status is TaskStatus.SUCCESS:
                self._deliver_task_result(event, task)
            elif task.status is TaskStatus.FAILED:
                self._repository.update_bili_event_fields(
                    event.id,
                    status=BiliEventStatus.TASK_FAILED.value,
                    delivery_status=BiliDeliveryStatus.FAILED.value,
                    error_message=task.error_message,
                )

    def _process_one_event(self, event: BiliEventRecord, mention: BiliMentionItem) -> None:
        """Validate one mention and create or reuse the corresponding summary task."""
        if not mention.mentions_self:
            self._repository.update_bili_event_fields(
                event.id,
                status=BiliEventStatus.IGNORED.value,
                error_message="通知未确认 @ 到当前账号。",
            )
            return

        extracted = extract_bilibili_video_source(mention.content)
        if extracted is None:
            self._repository.update_bili_event_fields(
                event.id,
                status=BiliEventStatus.IGNORED.value,
                error_message="评论中未包含 BV 号或 B 站视频链接。",
            )
            return

        source_url, bvid = extracted
        recipient = self._repository.find_bili_user_email_by_uid(mention.sender_mid)
        if recipient is None:
            self._repository.update_bili_event_fields(
                event.id,
                source_url=source_url,
                bvid=bvid,
                status=BiliEventStatus.FAILED.value,
                delivery_status=BiliDeliveryStatus.FAILED.value,
                error_message="该 B 站用户未绑定邮箱。",
            )
            return

        try:
            task = self._task_service.submit_task(source_url, reuse_success=True, send_mail=False)
        except Exception as exc:  # noqa: BLE001 - event table needs the visible failure reason.
            self._repository.update_bili_event_fields(
                event.id,
                source_url=source_url,
                bvid=bvid,
                status=BiliEventStatus.FAILED.value,
                delivery_status=BiliDeliveryStatus.FAILED.value,
                error_message=str(exc),
            )
            return

        next_status = BiliEventStatus.TASK_SUCCESS if task.status is TaskStatus.SUCCESS else BiliEventStatus.TASK_CREATED
        updated_event = self._repository.update_bili_event_fields(
            event.id,
            source_url=source_url,
            bvid=bvid,
            task_id=task.id,
            recipient_email=recipient.email,
            status=next_status.value,
            delivery_status=BiliDeliveryStatus.PENDING.value,
            error_message="",
        )
        if task.status is TaskStatus.SUCCESS:
            self._deliver_task_result(updated_event, task)

    def _deliver_task_result(self, event: BiliEventRecord, task: TaskRecord) -> None:
        """Send one completed task result to the email bound to this B 站 event."""
        if not event.recipient_email:
            self._repository.update_bili_event_fields(
                event.id,
                status=BiliEventStatus.FAILED.value,
                delivery_status=BiliDeliveryStatus.FAILED.value,
                error_message="该 B 站用户未绑定邮箱。",
            )
            return
        if not task.markdown_content or not task.markdown_file_path:
            self._repository.update_bili_event_fields(
                event.id,
                status=BiliEventStatus.TASK_FAILED.value,
                delivery_status=BiliDeliveryStatus.FAILED.value,
                error_message="关联任务缺少 Markdown 内容或文件路径。",
            )
            return

        try:
            self._mail_service.send_markdown(
                task.video_title or task.bvid,
                task.markdown_content,
                Path(task.markdown_file_path),
                to_email=event.recipient_email,
            )
        except Exception as exc:  # noqa: BLE001 - event delivery failure must be visible on the page.
            self._repository.update_bili_event_fields(
                event.id,
                status=BiliEventStatus.FAILED.value,
                delivery_status=BiliDeliveryStatus.FAILED.value,
                error_message=str(exc),
            )
            return

        self._repository.update_bili_event_fields(
            event.id,
            status=BiliEventStatus.TASK_SUCCESS.value,
            delivery_status=BiliDeliveryStatus.SENT.value,
            delivered_at=utc_now_text(),
            error_message="",
        )
        self._repository.update_task_fields(task.id, mail_status=MailStatus.SENT.value)


class BiliMentionPoller:
    """Background scheduler that periodically polls Bilibili @ notifications."""

    def __init__(
        self,
        config: AppConfig,
        auth_service: BiliAuthService,
        mention_api: BiliMentionApi,
        event_service: BiliEventService,
    ) -> None:
        self._config = config
        self._auth_service = auth_service
        self._mention_api = mention_api
        self._event_service = event_service
        self._logger = logging.getLogger("mysakura.bili")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = BiliListenerState(
            enabled=config.bili_enable_listener,
            running=False,
            login_status="DISABLED" if not config.bili_enable_listener else "UNKNOWN",
            account_mid="",
            account_name="",
            last_poll_at="",
            last_error="",
        )

    def start(self) -> None:
        """Start the polling thread when listener config is complete."""
        if not self._config.bili_enable_listener:
            return
        if not self._config.bili_cookie or not self._config.bili_self_mid:
            self._state.login_status = "CONFIG_ERROR"
            self._state.last_error = "请配置 BILI_COOKIE 和 BILI_SELF_MID。"
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="bili-mention-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background polling thread during application shutdown."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._state.running = False

    def snapshot(self) -> BiliListenerState:
        """Return current listener state for the local page."""
        return self._state

    def poll_once(self) -> None:
        """Run one auth check, one mention fetch, and one task-state synchronization."""
        try:
            account = self._auth_service.check_login()
            self._state.login_status = "LOGGED_IN"
            self._state.account_mid = account["mid"]
            self._state.account_name = account["uname"]
            mentions = self._mention_api.fetch_mentions()
            self._event_service.process_mentions(mentions)
            self._state.last_poll_at = utc_now_text()
            self._state.last_error = ""
        except Exception as exc:  # noqa: BLE001 - listener state must show the original reason.
            self._state.login_status = "ERROR"
            self._state.last_error = str(exc)
            self._logger.warning("Bilibili listener poll failed: %s", exc)

    def _run_loop(self) -> None:
        """Poll until the app shuts down, without blocking the summary worker."""
        self._state.running = True
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self._config.bili_poll_interval_seconds)
        self._state.running = False
