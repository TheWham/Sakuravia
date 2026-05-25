"""API and page checks for the local FastAPI app."""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path

from app.models import BiliDeliveryStatus, BiliEventStatus, MailStatus, TaskStatus


class BiliUserApiTests(unittest.TestCase):
    """Exercise V2 user APIs against an isolated SQLite database."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self._old_env = {
            key: os.environ.get(key)
            for key in ("SQLITE_PATH", "OUTPUT_DIR", "BILI_ENABLE_LISTENER")
        }
        root = Path(self.temp_dir.name)
        os.environ["SQLITE_PATH"] = str(root / "tasks.db")
        os.environ["OUTPUT_DIR"] = str(root / "output")
        os.environ["BILI_ENABLE_LISTENER"] = "false"

        import app.main as main_module

        self.main = importlib.reload(main_module)
        self.repo = self.main.app.state.repository

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def test_index_keeps_v1_flow_and_links_to_v2_users(self) -> None:
        response = _call_route(self.main.app, "/", "GET")
        html = response.body.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("解析并创建任务", html)
        self.assertIn("查看用户状态", html)
        self.assertNotIn("用户邮箱簿</div>", html)

    def test_v2_users_page_renders(self) -> None:
        response = _call_route(self.main.app, "/v2/users", "GET")
        html = response.body.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("V2 用户状态", html)
        self.assertIn("用户邮箱簿", html)

    def test_bili_users_api_keeps_user_fields_and_adds_stats(self) -> None:
        self.repo.upsert_bili_user_email("100", "测试用户", "target@example.com")
        event = self.repo.create_bili_event_if_absent("n1", "c1", "100", "测试用户", "@ai BV1ok")
        self.repo.update_bili_event_fields(
            event.id,
            status=BiliEventStatus.TASK_SUCCESS.value,
            delivery_status=BiliDeliveryStatus.SENT.value,
        )

        payload = _call_route(self.main.app, "/api/bili-users", "GET")

        self.assertEqual(payload["users"][0]["uid"], "100")
        self.assertEqual(payload["users"][0]["email"], "target@example.com")
        self.assertEqual(payload["users"][0]["stats"]["event_count"], 1)
        self.assertEqual(payload["users"][0]["stats"]["success_count"], 1)

    def test_bili_user_events_api_returns_events_and_task_summary(self) -> None:
        self.repo.upsert_bili_user_email("100", "测试用户", "target@example.com")
        task = self.repo.create_task("BV1linked", "BV1linked")
        self.repo.update_task_fields(
            task.id,
            status=TaskStatus.FAILED.value,
            mail_status=MailStatus.FAILED.value,
            video_title="关联任务",
            error_message="总结失败",
        )
        event = self.repo.create_bili_event_if_absent("n1", "c1", "100", "测试用户", "@ai BV1linked")
        self.repo.update_bili_event_fields(
            event.id,
            task_id=task.id,
            bvid="BV1linked",
            recipient_email="target@example.com",
            status=BiliEventStatus.TASK_FAILED.value,
            delivery_status=BiliDeliveryStatus.FAILED.value,
            error_message="任务失败",
        )

        payload = _call_route(self.main.app, "/api/bili-users/{uid}/events", "GET", "100")

        self.assertEqual(payload["uid"], "100")
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["sender_mid"], "100")
        self.assertEqual(payload["events"][0]["task"]["id"], task.id)
        self.assertEqual(payload["events"][0]["task"]["video_title"], "关联任务")
        self.assertEqual(payload["events"][0]["task"]["error_message"], "总结失败")


def _call_route(app, path: str, method: str, *args):
    """Call a FastAPI endpoint directly when TestClient is unavailable."""
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint(*args)
    raise AssertionError(f"route not found: {method} {path}")


if __name__ == "__main__":
    unittest.main()
