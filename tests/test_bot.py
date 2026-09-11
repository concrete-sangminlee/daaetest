"""Offline unit tests for the pure functions in bot.py.

These tests import the REAL functions from bot.py (not reimplementations). Because
the sandbox cannot install requests/dotenv/notion-client, tests/_stubs.py registers
minimal stand-in modules before bot is imported. All assertions here exercise pure,
network-free logic.

Run with:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make the repo root importable and install offline dependency stubs before
# importing bot (bot.py imports requests/dotenv at module top level).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._stubs import install_stub_dependencies

install_stub_dependencies()

import bot  # noqa: E402  (import after stub installation is intentional)


BASE_URL = "https://architecture.snu.ac.kr"


class NormalizeNoticeTests(unittest.TestCase):
    def test_normalizes_getnotices_item(self):
        item = {
            "id": 123,
            "title": "신입생 &amp; 재학생 <b>OT</b> 안내",
            "category": "학사",
            "ctype": "notice",
            "post_date": "2026.06.08",
        }
        result = bot.normalize_notice(item, base_url=BASE_URL)

        self.assertEqual(result["id"], 123)
        self.assertEqual(result["title"], "신입생 & 재학생 OT 안내")
        self.assertEqual(result["link"], "https://architecture.snu.ac.kr/post/123")
        self.assertEqual(result["post_date"], "2026.06.08")
        self.assertEqual(result["ctype"], "notice")
        self.assertEqual(
            set(result.keys()), {"id", "title", "link", "post_date", "ctype"}
        )

    def test_link_uses_base_url_without_trailing_slash(self):
        result = bot.normalize_notice(
            {"id": 7, "title": "t", "post_date": "", "ctype": ""},
            base_url="https://architecture.snu.ac.kr/",
        )
        self.assertEqual(result["link"], "https://architecture.snu.ac.kr/post/7")

    def test_missing_id_yields_empty_link(self):
        result = bot.normalize_notice(
            {"title": "no id", "post_date": "2026.01.01"}, base_url=BASE_URL
        )
        self.assertEqual(result["id"], 0)
        self.assertEqual(result["link"], "")


class FormatPostDateTests(unittest.TestCase):
    def test_formats_dotted_date(self):
        self.assertEqual(bot._format_post_date("2026.06.08"), "2026. 6. 8.")

    def test_formats_single_digit_components(self):
        self.assertEqual(bot._format_post_date("2026.1.2"), "2026. 1. 2.")

    def test_passthrough_for_unparseable(self):
        self.assertEqual(bot._format_post_date("nope"), "nope")

    def test_empty_string(self):
        self.assertEqual(bot._format_post_date(""), "")


class CleanTextTests(unittest.TestCase):
    def test_strips_tags_and_unescapes_entities(self):
        self.assertEqual(
            bot._clean_text("<b>Hello</b> &amp; welcome"), "Hello & welcome"
        )

    def test_collapses_whitespace(self):
        self.assertEqual(bot._clean_text("a   b\n\tc"), "a b c")

    def test_handles_empty(self):
        self.assertEqual(bot._clean_text(""), "")


class SlackEscapeTests(unittest.TestCase):
    def test_escapes_amp_lt_gt(self):
        self.assertEqual(bot._slack_escape("a & b < c > d"), "a &amp; b &lt; c &gt; d")

    def test_replaces_pipe(self):
        self.assertEqual(bot._slack_escape("left|right"), "left｜right")

    def test_empty(self):
        self.assertEqual(bot._slack_escape(""), "")


class ParseBoolTests(unittest.TestCase):
    def test_truthy_values(self):
        for v in ["1", "true", "T", "yes", "y", "on"]:
            self.assertTrue(bot._parse_bool(v), v)

    def test_falsy_values(self):
        for v in ["0", "false", "no", "off"]:
            self.assertFalse(bot._parse_bool(v), v)

    def test_default_on_none_and_unknown(self):
        self.assertFalse(bot._parse_bool(None))
        self.assertTrue(bot._parse_bool(None, default=True))
        self.assertTrue(bot._parse_bool("maybe", default=True))


class BuildSlackSummaryTextTests(unittest.TestCase):
    def _posts(self):
        return [
            {
                "id": 1,
                "title": "첫 번째 공지",
                "link": "https://architecture.snu.ac.kr/post/1",
                "post_date": "2026.06.08",
                "ctype": "notice",
            },
            {
                "id": 2,
                "title": "두 번째 공지",
                "link": "https://architecture.snu.ac.kr/post/2",
                "post_date": "2026.06.09",
                "ctype": "notice",
            },
        ]

    def test_header_has_count_and_one_line_per_post(self):
        text = bot.build_slack_summary_text(
            posts=self._posts(), feed_name="건축학과", emoji="📰", is_test=False
        )
        lines = text.split("\n")
        # header + one line per post
        self.assertEqual(len(lines), 3)
        self.assertIn("새 글 2개", lines[0])
        self.assertIn("📰", lines[0])
        self.assertIn("첫 번째 공지", lines[1])
        self.assertIn("2026. 6. 8.", lines[1])
        self.assertIn("두 번째 공지", lines[2])

    def test_link_embedded_as_mrkdwn(self):
        text = bot.build_slack_summary_text(
            posts=self._posts()[:1], feed_name="건축학과", emoji="📰", is_test=False
        )
        self.assertIn("<https://architecture.snu.ac.kr/post/1|", text)


class BuildSlackAttachmentsTests(unittest.TestCase):
    def _posts(self):
        return [
            {
                "id": 10,
                "title": "공지 A",
                "link": "https://architecture.snu.ac.kr/post/10",
                "post_date": "2026.06.08",
                "ctype": "notice",
            }
        ]

    def test_returns_single_attachment_with_navy_color_and_blocks(self):
        attachments = bot.build_slack_attachments(
            posts=self._posts(),
            feed_name="건축학과",
            emoji="📰",
            is_test=False,
            base_url=BASE_URL,
        )
        self.assertIsInstance(attachments, list)
        self.assertEqual(len(attachments), 1)
        att = attachments[0]
        self.assertEqual(att["color"], "#003876")
        self.assertIsInstance(att["blocks"], list)
        self.assertTrue(len(att["blocks"]) > 0)

    def test_test_flag_adds_test_prefix(self):
        attachments = bot.build_slack_attachments(
            posts=self._posts(),
            feed_name="건축학과",
            emoji="📰",
            is_test=True,
            base_url=BASE_URL,
        )
        header_text = attachments[0]["blocks"][0]["text"]["text"]
        self.assertIn("[TEST]", header_text)


class BuildPingAttachmentsTests(unittest.TestCase):
    def test_returns_green_attachment(self):
        attachments = bot.build_ping_attachments(
            feed_name="건축학과", base_url=BASE_URL
        )
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["color"], "#2eb67d")
        self.assertIsInstance(attachments[0]["blocks"], list)


class StateRoundTripTests(unittest.TestCase):
    def test_load_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "does_not_exist.json"
            state = bot.load_state(path)
            self.assertEqual(state, {"version": 2, "streams": {}})

    def test_save_then_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = {
                "version": 2,
                "streams": {
                    bot.make_stream_key(BASE_URL): {
                        "seen_ids": [3, 2, 1],
                        "updated_at": "2026-06-08T00:00:00+00:00",
                    }
                },
            }
            bot.save_state(path, state)
            self.assertTrue(path.exists())
            loaded = bot.load_state(path)
            self.assertEqual(loaded, state)

    def test_make_stream_key(self):
        self.assertEqual(
            bot.make_stream_key(BASE_URL),
            "https://architecture.snu.ac.kr/rest/activities/getNotices",
        )


class PruneSeenIdsTests(unittest.TestCase):
    def test_dedupes_and_sorts_desc(self):
        self.assertEqual(bot._prune_seen_ids([1, 3, 2, 3, 1]), [3, 2, 1])

    def test_caps_to_max(self):
        big = list(range(bot.MAX_SEEN_IDS + 50))
        pruned = bot._prune_seen_ids(big)
        self.assertEqual(len(pruned), bot.MAX_SEEN_IDS)
        # keeps the largest ids
        self.assertEqual(pruned[0], bot.MAX_SEEN_IDS + 49)


if __name__ == "__main__":
    unittest.main()
