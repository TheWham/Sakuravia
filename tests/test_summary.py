"""Summary prompt tests."""

from __future__ import annotations

import unittest

from app.services.summary import SUMMARY_TEMPLATE


class SummaryPromptTests(unittest.TestCase):
    """Keep the summary style closer to a useful note than a transcript recap."""

    def test_prompt_requires_topic_note_and_tables_for_list_videos(self) -> None:
        prompt = SUMMARY_TEMPLATE.format(
            title="10个热门Prompt，20秒一次全盘点！",
            bvid="BV1test",
            uploader="测试UP",
            duration=22,
            tags="AI生图、Prompt",
            description="适合短视频和小红书创作",
            transcript="棒球赛直播提示词，正装职业照提示词",
        )

        self.assertIn("主题型标题", prompt)
        self.assertIn("核心清单表", prompt)
        self.assertIn("主要用途", prompt)
        self.assertIn("技术实践类", prompt)
        self.assertIn("背景问题", prompt)
        self.assertIn("📌 视频信息", prompt)
        self.assertIn("⚠️ 反例与坑点", prompt)
        self.assertIn("💡 AI 理解与延伸", prompt)
        self.assertIn("固定使用这些视觉符号", prompt)
        self.assertIn("视频简介：适合短视频和小红书创作", prompt)
        self.assertIn("不要输出“# 视频标题”", prompt)
