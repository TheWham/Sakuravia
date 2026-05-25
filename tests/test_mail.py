"""Mail message assembly tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.services.mail import MailService


class MailServiceTests(unittest.TestCase):
    """Verify subject, recipient and attachment shape without touching SMTP."""

    def test_build_message_attaches_markdown_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            attachment = Path(temp_dir) / "result.md"
            attachment.write_text("# 标题", encoding="utf-8")

            config = AppConfig(
                app_host="127.0.0.1",
                app_port=8000,
                sqlite_path=Path(temp_dir) / "tasks.db",
                output_dir=Path(temp_dir),
                audio_dir=Path(temp_dir) / "audio",
                tmp_dir=Path(temp_dir) / "tmp",
                yt_dlp_bin="yt-dlp",
                ffmpeg_bin="ffmpeg",
                groq_api_key="test",
                groq_asr_model="whisper-large-v3-turbo",
                deepseek_base_url="https://api.deepseek.com",
                deepseek_api_key="test",
                deepseek_model="deepseek-chat",
                smtp_host="smtp.example.com",
                smtp_port=465,
                smtp_username="user",
                smtp_password="password",
                smtp_use_ssl=True,
                mail_from="from@example.com",
                mail_to="to@example.com",
            )
            service = MailService(config)

            markdown = "\n".join(
                [
                    "# 标题",
                    "",
                    "## 📌 视频信息",
                    "正文内容",
                    "",
                    "## ⚠️ 反例与坑点",
                    "⚠️ 不要把推断写成事实。",
                    "",
                    "## 🚀 可执行建议",
                    "🚀 先从一个真实任务试跑。",
                    "",
                    "## 💡 AI 理解与延伸",
                    "💡 这部分是基于内容的合理延伸。",
                    "",
                    "| 序号 | Prompt 类型 |",
                    "| --- | --- |",
                    "| 1 | 棒球赛直播提示词 |",
                ]
            )
            message = service.build_message("测试视频", markdown, attachment)

            self.assertEqual(message["To"], "to@example.com")
            self.assertIn("测试视频", message["Subject"])
            self.assertIn("正文内容", message.get_body(preferencelist=("plain",)).get_content())
            html_body = message.get_body(preferencelist=("html",)).get_content()
            self.assertIn("<h1", html_body)
            self.assertIn("<table", html_body)
            self.assertIn("#fff7ed", html_body)
            self.assertIn("#f0fdf4", html_body)
            self.assertIn("#eff6ff", html_body)
            self.assertIn("棒球赛直播提示词", html_body)
            attachments = list(message.iter_attachments())
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0].get_filename(), "result.md")
            self.assertEqual(attachments[0].get_payload(decode=True).decode("utf-8").strip(), "# 标题")
