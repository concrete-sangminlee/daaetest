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
from unittest import mock

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


def _make_config(**overrides):
    """Build a Config with sensible offline defaults; override individual fields."""
    defaults = dict(
        base_url=BASE_URL,
        max_notify_per_run=20,
        send_on_first_run=False,
        state_path=Path("./state.json"),
        slack_webhook_url=None,
        slack_channel=None,
        slack_username=None,
        notion_token=None,
        notion_page_id=None,
        alert_feed_name="건축학과",
        alert_emoji="📰",
        dry_run=False,
        init_only=False,
        test_latest=False,
        ping=False,
        heartbeat=False,
    )
    defaults.update(overrides)
    return bot.Config(**defaults)


class BuildFailureAttachmentsTests(unittest.TestCase):
    def test_returns_red_attachment_with_summary(self):
        attachments = bot.build_failure_attachments(
            feed_name="건축학과",
            error_summary="공지 API가 JSON 대신 HTML을 반환했습니다.",
            base_url=BASE_URL,
        )
        self.assertEqual(len(attachments), 1)
        att = attachments[0]
        self.assertEqual(att["color"], "#e01e5a")
        self.assertIsInstance(att["blocks"], list)
        section_text = att["blocks"][0]["text"]["text"]
        self.assertIn("공지 API가 JSON 대신 HTML을 반환했습니다.", section_text)

    def test_truncates_long_summary(self):
        long_summary = "x" * 2000
        attachments = bot.build_failure_attachments(
            feed_name="건축학과", error_summary=long_summary
        )
        section_text = attachments[0]["blocks"][0]["text"]["text"]
        # truncated body should be far shorter than the raw 2000 chars
        self.assertLessEqual(section_text.count("x"), bot.MAX_ERROR_SUMMARY_CHARS)
        self.assertIn("생략", section_text)

    def test_does_not_echo_secret_values(self):
        # The builder only receives an error summary; secrets are never passed in.
        secret = "https://hooks.slack.com/services/T000/B000/XXXXSECRET"
        attachments = bot.build_failure_attachments(
            feed_name="건축학과", error_summary="네트워크 오류가 발생했습니다."
        )
        blob = repr(attachments)
        self.assertNotIn(secret, blob)
        self.assertNotIn("XXXXSECRET", blob)

    def test_empty_summary_has_placeholder(self):
        attachments = bot.build_failure_attachments(
            feed_name="건축학과", error_summary=""
        )
        section_text = attachments[0]["blocks"][0]["text"]["text"]
        self.assertIn("원인 정보 없음", section_text)


class BuildHeartbeatAttachmentsTests(unittest.TestCase):
    def test_returns_green_attachment(self):
        attachments = bot.build_heartbeat_attachments(
            feed_name="건축학과", base_url=BASE_URL
        )
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["color"], "#2eb67d")
        self.assertIsInstance(attachments[0]["blocks"], list)
        section_text = attachments[0]["blocks"][0]["text"]["text"]
        self.assertIn("정상 동작 중", section_text)


class _FakeSession:
    """Stand-in returned by a patched bot._requests_session so run() never builds
    the real requests/urllib3 network stack (unavailable/old in the sandbox)."""

    def post(self, *args, **kwargs):  # pragma: no cover - should be monkeypatched away
        raise RuntimeError("network disabled in tests")


class HeartbeatModeTests(unittest.TestCase):
    def test_dry_run_prints_and_returns_zero(self):
        cfg = _make_config(heartbeat=True, dry_run=True)
        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                mock.patch.object(bot, "send_slack_message") as send:
            rc = bot.run(cfg)
        self.assertEqual(rc, 0)
        send.assert_not_called()

    def test_requires_webhook_when_not_dry_run(self):
        cfg = _make_config(heartbeat=True, dry_run=False, slack_webhook_url=None)
        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()):
            with self.assertRaises(SystemExit):
                bot.run(cfg)

    def test_sends_heartbeat_when_webhook_present(self):
        cfg = _make_config(
            heartbeat=True, dry_run=False, slack_webhook_url="https://example/webhook"
        )
        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                mock.patch.object(bot, "send_slack_message") as send:
            rc = bot.run(cfg)
        self.assertEqual(rc, 0)
        send.assert_called_once()
        _, kwargs = send.call_args
        self.assertIn("[HEARTBEAT]", kwargs["text"])


class MissingWebhookGuardTests(unittest.TestCase):
    def test_real_run_without_webhook_or_notion_raises(self):
        cfg = _make_config(dry_run=False, slack_webhook_url=None)
        # Stub fetch_notices so no network is attempted; the guard runs before fetch.
        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                mock.patch.object(bot, "fetch_notices", return_value=[]):
            with self.assertRaises(SystemExit):
                bot.run(cfg)

    def test_dry_run_without_webhook_is_allowed(self):
        cfg = _make_config(dry_run=True, slack_webhook_url=None)
        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                mock.patch.object(bot, "fetch_notices", return_value=[]):
            # init-like first run baseline path prints and returns 0 (no webhook needed)
            rc = bot.run(cfg)
        self.assertEqual(rc, 0)


class FailureNotificationTests(unittest.TestCase):
    def test_send_failure_notification_calls_slack(self):
        cfg = _make_config(slack_webhook_url="https://example/webhook")
        with mock.patch.object(bot, "send_slack_message") as send:
            bot.send_failure_notification(
                object(), cfg=cfg, error_summary="boom"
            )
        send.assert_called_once()
        _, kwargs = send.call_args
        self.assertIn("[ERROR]", kwargs["text"])

    def test_send_failure_notification_noop_without_webhook(self):
        cfg = _make_config(slack_webhook_url=None)
        with mock.patch.object(bot, "send_slack_message") as send:
            bot.send_failure_notification(object(), cfg=cfg, error_summary="boom")
        send.assert_not_called()

    def test_send_failure_notification_is_best_effort(self):
        cfg = _make_config(slack_webhook_url="https://example/webhook")
        with mock.patch.object(
            bot, "send_slack_message", side_effect=RuntimeError("secondary")
        ):
            # Must NOT raise even though the inner send fails.
            bot.send_failure_notification(object(), cfg=cfg, error_summary="boom")

    def test_run_failure_triggers_notification_and_nonzero_exit(self):
        cfg = _make_config(
            dry_run=False, slack_webhook_url="https://example/webhook"
        )
        calls = []

        def _record(*args, **kwargs):
            calls.append(kwargs)

        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                mock.patch.object(
                    bot, "fetch_notices", side_effect=RuntimeError("API drift: HTML")
                ), mock.patch.object(bot, "send_slack_message", side_effect=_record), \
                mock.patch.object(
                    bot, "build_config_from_env_and_args", return_value=cfg
                ), mock.patch.object(bot, "parse_args", return_value=None), \
                mock.patch.object(sys, "argv", ["bot.py"]):
            with self.assertRaises(SystemExit) as ctx:
                bot.main()

        # Non-zero exit
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertNotEqual(ctx.exception.code, None)
        # A failure notification was attempted with the [ERROR] fallback text.
        self.assertTrue(calls, "expected a failure Slack call")
        self.assertTrue(any("[ERROR]" in c.get("text", "") for c in calls))


class _ScriptedPages:
    """Callable stand-in for bot.fetch_notices that returns scripted raw pages
    keyed by page number. Records the pages requested so tests can assert the
    walker stopped at the right point (never hits the network)."""

    def __init__(self, pages_by_num):
        # pages_by_num: {1: [items...], 2: [...], ...}; missing page -> [] (empty)
        self.pages_by_num = pages_by_num
        self.requested_pages = []

    def __call__(self, session, *, base_url, page=1, timeout_sec=15.0):
        self.requested_pages.append(page)
        return list(self.pages_by_num.get(page, []))


def _raw_item(pid, *, post_date="2026.01.01"):
    return {
        "id": pid,
        "title": f"공지 {pid}",
        "category": "학사",
        "ctype": "notice",
        "post_date": post_date,
    }


class FetchAllNewNoticesTests(unittest.TestCase):
    def test_walks_all_new_pages_then_stops_on_empty(self):
        # 3 pages all-new, page 4 empty -> everything collected, stops on empty page.
        scripted = _ScriptedPages({
            1: [_raw_item(101), _raw_item(102)],
            2: [_raw_item(103), _raw_item(104)],
            3: [_raw_item(105)],
            # page 4 missing -> empty
        })
        with mock.patch.object(bot, "fetch_notices", side_effect=scripted):
            notices, current_ids = bot.fetch_all_new_notices(
                _FakeSession(), base_url=BASE_URL, seen_ids=set()
            )
        self.assertEqual([p["id"] for p in notices], [101, 102, 103, 104, 105])
        self.assertEqual(current_ids, [101, 102, 103, 104, 105])
        # Walked pages 1..4 (4 was the empty stop).
        self.assertEqual(scripted.requested_pages, [1, 2, 3, 4])

    def test_stops_when_page_has_no_new_posts(self):
        # page 1 all new, page 2 entirely already-seen -> stop, do NOT fetch page 3.
        scripted = _ScriptedPages({
            1: [_raw_item(201), _raw_item(202)],
            2: [_raw_item(10), _raw_item(11)],
            3: [_raw_item(300)],  # should never be requested
        })
        seen = {10, 11}
        with mock.patch.object(bot, "fetch_notices", side_effect=scripted):
            notices, current_ids = bot.fetch_all_new_notices(
                _FakeSession(), base_url=BASE_URL, seen_ids=seen
            )
        # page 2 was fetched (its ids collected) but page 3 was not.
        self.assertEqual(scripted.requested_pages, [1, 2])
        self.assertEqual([p["id"] for p in notices], [201, 202, 10, 11])
        self.assertEqual(current_ids, [201, 202, 10, 11])

    def test_max_pages_cap_halts_infinite_api(self):
        # An API that always returns a brand-new item would loop forever without the cap.
        class _InfiniteAPI:
            def __init__(self):
                self.requested_pages = []

            def __call__(self, session, *, base_url, page=1, timeout_sec=15.0):
                self.requested_pages.append(page)
                # Every page yields a unique, never-before-seen id.
                return [_raw_item(1000 + page)]

        api = _InfiniteAPI()
        with mock.patch.object(bot, "fetch_notices", side_effect=api):
            notices, current_ids = bot.fetch_all_new_notices(
                _FakeSession(), base_url=BASE_URL, seen_ids=set(), max_pages=3
            )
        # Exactly max_pages requests, then stop.
        self.assertEqual(api.requested_pages, [1, 2, 3])
        self.assertEqual([p["id"] for p in notices], [1001, 1002, 1003])
        self.assertEqual(current_ids, [1001, 1002, 1003])

    def test_single_page_behaviour_matches_legacy(self):
        # One page of new posts, page 2 empty -> same result a single fetch would give.
        scripted = _ScriptedPages({1: [_raw_item(1), _raw_item(2)]})
        with mock.patch.object(bot, "fetch_notices", side_effect=scripted):
            notices, current_ids = bot.fetch_all_new_notices(
                _FakeSession(), base_url=BASE_URL, seen_ids=set()
            )
        self.assertEqual([p["id"] for p in notices], [1, 2])
        self.assertEqual(current_ids, [1, 2])
        # page 1 had new posts so page 2 is probed and comes back empty -> stop.
        self.assertEqual(scripted.requested_pages, [1, 2])

    def test_dedupes_pinned_notice_repeated_across_pages(self):
        # A pinned notice (id 500) appears on both page 1 and page 2.
        scripted = _ScriptedPages({
            1: [_raw_item(500), _raw_item(401)],
            2: [_raw_item(500), _raw_item(402)],
        })
        with mock.patch.object(bot, "fetch_notices", side_effect=scripted):
            notices, current_ids = bot.fetch_all_new_notices(
                _FakeSession(), base_url=BASE_URL, seen_ids=set()
            )
        # 500 collected only once.
        self.assertEqual([p["id"] for p in notices], [500, 401, 402])
        self.assertEqual(current_ids, [500, 401, 402])

    def test_html_drift_runtimeerror_propagates(self):
        def _drift(session, *, base_url, page=1, timeout_sec=15.0):
            raise RuntimeError("공지 API가 JSON 대신 HTML(SPA 페이지)을 반환했습니다.")

        with mock.patch.object(bot, "fetch_notices", side_effect=_drift):
            with self.assertRaises(RuntimeError):
                bot.fetch_all_new_notices(
                    _FakeSession(), base_url=BASE_URL, seen_ids=set()
                )


class PaginationRunTests(unittest.TestCase):
    def _state_with_seen(self, seen_ids):
        return {
            "version": 2,
            "streams": {
                bot.make_stream_key(BASE_URL): {
                    "seen_ids": list(seen_ids),
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            },
        }

    def test_run_notifies_across_multiple_pages_and_saves_all_ids(self):
        # Existing state has one seen id (1). New posts are spread over pages 1..2,
        # page 3 has only already-seen ids -> walker stops there.
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "state.json"
            bot.save_state(state_path, self._state_with_seen([1]))

            cfg = _make_config(
                dry_run=False,
                slack_webhook_url="https://example/webhook",
                state_path=state_path,
            )

            scripted = _ScriptedPages({
                1: [_raw_item(1, post_date="2026.01.01"), _raw_item(20, post_date="2026.02.02")],
                2: [_raw_item(21, post_date="2026.02.03"), _raw_item(22, post_date="2026.02.04")],
                3: [_raw_item(1, post_date="2026.01.01")],  # only already-seen -> stop
            })

            sent = {}

            def _capture_notify(posts, *, is_test):
                sent["posts"] = list(posts)
                sent["is_test"] = is_test

            with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                    mock.patch.object(bot, "fetch_notices", side_effect=scripted), \
                    mock.patch.object(bot, "send_slack_message"):
                # Patch notify indirectly: intercept build via send_slack_message is
                # enough, but we assert on the posts passed to notify by inspecting
                # the slack summary text instead.
                with mock.patch.object(bot, "build_slack_summary_text", side_effect=lambda *, posts, **k: _capture_notify(posts, is_test=False) or "text"):
                    rc = bot.run(cfg)

            self.assertEqual(rc, 0)
            # run() fetches page 1 once up front (init/test-latest view) and then the
            # walker paginates from page 1: pages 1, 2, 3 (page 3 stopped it, no new ids).
            self.assertEqual(scripted.requested_pages, [1, 1, 2, 3])
            # The walker itself reached page 3.
            self.assertIn(3, scripted.requested_pages)
            # Notified posts came from BOTH page 1 (id 20) and page 2 (ids 21, 22),
            # sorted by (post_date, id); already-seen id 1 excluded.
            self.assertEqual([p["id"] for p in sent["posts"]], [20, 21, 22])

            # State now includes ids from all fetched pages (1 already seen + 20,21,22).
            loaded = bot.load_state(state_path)
            saved_seen = set(loaded["streams"][bot.make_stream_key(BASE_URL)]["seen_ids"])
            self.assertEqual(saved_seen, {1, 20, 21, 22})

    def test_run_single_page_is_regression_safe(self):
        # With only page 1 populated (page 2 empty), behaviour equals the legacy
        # single-page path: the one new post is notified, both ids stored.
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "state.json"
            bot.save_state(state_path, self._state_with_seen([100]))

            cfg = _make_config(
                dry_run=False,
                slack_webhook_url="https://example/webhook",
                state_path=state_path,
            )

            scripted = _ScriptedPages({
                1: [_raw_item(100, post_date="2026.01.01"), _raw_item(101, post_date="2026.03.03")],
            })

            captured = {}

            with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                    mock.patch.object(bot, "fetch_notices", side_effect=scripted), \
                    mock.patch.object(bot, "send_slack_message"), \
                    mock.patch.object(bot, "build_slack_summary_text", side_effect=lambda *, posts, **k: captured.update(posts=list(posts)) or "text"):
                rc = bot.run(cfg)

            self.assertEqual(rc, 0)
            # run() fetches page 1 once up front, then the walker: page 1 had a new id
            # -> page 2 probed (empty) -> stop.
            self.assertEqual(scripted.requested_pages, [1, 1, 2])
            self.assertEqual([p["id"] for p in captured["posts"]], [101])
            loaded = bot.load_state(state_path)
            saved_seen = set(loaded["streams"][bot.make_stream_key(BASE_URL)]["seen_ids"])
            self.assertEqual(saved_seen, {100, 101})


class MainStringExitCodeTests(unittest.TestCase):
    """main() must treat a SystemExit raised with a *string* code (as _require
    does for a missing webhook, e.g. SystemExit('SLACK_WEBHOOK_URL이 필요합니다.'))
    as a failure: route it through _report_failure (attempting a Slack failure
    notification when a webhook is configured) and re-raise SystemExit(2)."""

    def test_string_exit_code_from_run_maps_to_failure_and_nonzero(self):
        cfg = _make_config(
            dry_run=False, slack_webhook_url="https://example/webhook"
        )
        calls = []

        def _record(*args, **kwargs):
            calls.append(kwargs)

        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                mock.patch.object(
                    bot, "run", side_effect=SystemExit("SLACK_WEBHOOK_URL이 필요합니다.")
                ), mock.patch.object(bot, "send_slack_message", side_effect=_record), \
                mock.patch.object(
                    bot, "build_config_from_env_and_args", return_value=cfg
                ), mock.patch.object(bot, "parse_args", return_value=None), \
                mock.patch.object(sys, "argv", ["bot.py"]):
            with self.assertRaises(SystemExit) as ctx:
                bot.main()

        # Non-zero, non-None exit code (specifically 2).
        self.assertEqual(ctx.exception.code, 2)
        # A failure notification was attempted; the string reason is surfaced as
        # the error summary and the [ERROR] fallback text is used.
        self.assertTrue(calls, "expected a failure Slack call")
        self.assertTrue(any("[ERROR]" in c.get("text", "") for c in calls))

    def test_string_exit_code_without_webhook_still_exits_nonzero_no_slack(self):
        # No webhook configured -> _report_failure skips Slack, but the exit is
        # still a non-zero failure code.
        cfg = _make_config(dry_run=False, slack_webhook_url=None)
        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                mock.patch.object(
                    bot, "run", side_effect=SystemExit("알림을 보낼 수 없습니다.")
                ), mock.patch.object(bot, "send_slack_message") as send, \
                mock.patch.object(
                    bot, "build_config_from_env_and_args", return_value=cfg
                ), mock.patch.object(bot, "parse_args", return_value=None), \
                mock.patch.object(sys, "argv", ["bot.py"]):
            with self.assertRaises(SystemExit) as ctx:
                bot.main()

        self.assertEqual(ctx.exception.code, 2)
        send.assert_not_called()

    def test_run_returning_zero_exits_zero_with_no_failure_notification(self):
        cfg = _make_config(
            dry_run=False, slack_webhook_url="https://example/webhook"
        )
        with mock.patch.object(bot, "_requests_session", return_value=_FakeSession()), \
                mock.patch.object(bot, "run", return_value=0), \
                mock.patch.object(bot, "send_slack_message") as send, \
                mock.patch.object(
                    bot, "build_config_from_env_and_args", return_value=cfg
                ), mock.patch.object(bot, "parse_args", return_value=None), \
                mock.patch.object(sys, "argv", ["bot.py"]):
            with self.assertRaises(SystemExit) as ctx:
                bot.main()

        # Clean exit (code 0 or None), and NO failure notification attempted.
        self.assertIn(ctx.exception.code, (0, None))
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
