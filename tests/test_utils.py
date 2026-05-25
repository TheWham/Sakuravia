"""Focused tests for input parsing and artifact naming."""

from __future__ import annotations

import unittest

from app.utils import ValidationError, normalize_bilibili_source, sanitize_filename


class NormalizeSourceTests(unittest.TestCase):
    """Validate the two accepted input styles and the main rejection paths."""

    def test_accepts_bvid_directly(self) -> None:
        self.assertEqual(normalize_bilibili_source("BV1xx411c7mD"), "BV1xx411c7mD")

    def test_extracts_bvid_from_url(self) -> None:
        url = "https://www.bilibili.com/video/BV1xx411c7mD?p=1"
        self.assertEqual(normalize_bilibili_source(url), "BV1xx411c7mD")

    def test_rejects_non_bilibili_link(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_bilibili_source("https://example.com/video/123")


class FilenameTests(unittest.TestCase):
    """Check filename cleanup on Windows-unfriendly titles."""

    def test_sanitize_filename_replaces_forbidden_chars(self) -> None:
        self.assertEqual(sanitize_filename('A:B/C*D?"E<F>G|'), "A_B_C_D_E_F_G_")
