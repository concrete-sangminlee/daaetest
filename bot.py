#!/usr/bin/env python3
"""
architecture.snu.ac.kr 새 글을 감지해 Slack으로 알림을 보내는 폴링(polling) 스크립트입니다.

핵심 아이디어
- 대상 사이트가 WordPress이므로, HTML 스크래핑 대신 WordPress REST API(/wp-json/wp/v2/posts)를 사용합니다.
- 마지막으로 알림 보낸 지점을 state.json에 (date_gmt, id) 커서(cursor)로 저장해 중복 알림을 방지합니다.
- 크론(cron)이나 스케줄러(GitHub Actions 등)로 주기 실행하면 "새 글 올라오면 알림"처럼 동작합니다.

실행 예시
  1) 최초 기준점만 저장(스팸 방지)
     python3 bot.py --init
  2) 평상시 실행(새 글 있으면 Slack 전송)
     python3 bot.py
  3) Slack 전송 없이 출력만(dry-run)
     python3 bot.py --dry-run
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from email.utils import format_datetime
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv


WP_POSTS_PATH = "/wp-json/wp/v2/posts"
WP_CATEGORIES_PATH = "/wp-json/wp/v2/categories"


def _parse_bool(value: Optional[str], *, default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _parse_csv_str(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",")]
    return [p for p in parts if p]


def _parse_csv_int(value: Optional[str]) -> List[int]:
    out: List[int] = []
    for p in _parse_csv_str(value):
        try:
            out.append(int(p))
        except ValueError:
            raise ValueError(f"정수로 파싱할 수 없는 값이 있습니다: {p!r}") from None
    return out


def _clean_text(text: str) -> str:
    """
    WordPress title.rendered는 HTML일 수 있어 Slack에 보기 좋게 정리합니다.
    """
    t = html.unescape(text or "")
    t = re.sub(r"<[^>]+>", "", t)  # 매우 단순한 tag strip
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_wp_date_gmt(date_gmt: str) -> datetime:
    """
    WordPress REST API의 date_gmt는 보통 'YYYY-MM-DDTHH:MM:SS' 형태(타임존 오프셋 없음)입니다.
    이를 UTC aware datetime으로 변환합니다.
    """
    # date_gmt에 'Z'가 붙어올 수도 있어 둘 다 처리합니다.
    s = (date_gmt or "").strip()
    if not s:
        raise ValueError("date_gmt가 비어 있습니다.")
    if s.endswith("Z"):
        s = s[:-1]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _format_dt_z(dt: datetime) -> str:
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slack_escape(text: str) -> str:
    """
    Slack mrkdwn에서 링크 텍스트로 쓸 때 깨질 수 있는 문자들을 최소한으로 이스케이프합니다.
    """
    t = text or ""
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = t.replace("|", "｜")  # 링크 텍스트 구분자 충돌 방지
    return t


def _format_rfc2822_utc(date_gmt: str) -> str:
    """
    예: Fri, 02 Jan 2026 11:00:58 +0000
    - 사용자 요청 포맷에 맞춰 RFC 2822(=RSS pubDate 느낌)로 표기합니다.
    """
    dt_utc = _parse_wp_date_gmt(date_gmt)
    dt_utc = dt_utc.replace(microsecond=0).astimezone(timezone.utc)
    return format_datetime(dt_utc, usegmt=False)


def build_slack_summary_text(
    *,
    posts: List[Dict[str, Any]],
    feed_name: str,
    emoji: str,
    is_test: bool,
) -> str:
    """
    요청하신 텍스트 포맷으로 1개의 메시지에 N개 글을 요약합니다.

    예:
      :신문: 협동과정 인공지능 전공 새 글 1개
      - 제목  (Fri, 02 Jan 2026 11:00:58 +0000)
    """
    count = len(posts)
    # 사용자 요청: 헤더에 [TEST]는 표시하지 않음
    # 사용자 요청: '건축학과'는 볼드 처리
    display_name = (feed_name or "").strip()
    display_name = _slack_escape(display_name)
    display_name = display_name.replace("건축학과", "*건축학과*")
    header = f"{emoji} {display_name} 새 글 {count}개"

    lines = [header]
    for p in posts:
        title = _clean_text(str(p.get("title", {}).get("rendered", "")))
        link = str(p.get("link", "")).strip()
        date_gmt = str(p.get("date_gmt", "")).strip()

        safe_title = _slack_escape(title or "(제목 없음)")
        safe_link = link

        # 제목은 URL을 노출하지 않고 클릭 가능하게 만듭니다.
        title_md = f"<{safe_link}|{safe_title}>" if safe_link else safe_title
        date_str = _format_rfc2822_utc(date_gmt) if date_gmt else ""

        if date_str:
            lines.append(f"- {title_md}  ({date_str})")
        else:
            lines.append(f"- {title_md}")

    return "\n".join(lines)


def _requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "daae-slack-bot/1.0 (+https://architecture.snu.ac.kr)",
            "Accept": "application/json",
        }
    )
    return s


def _wp_get_json(
    session: requests.Session,
    *,
    base_url: str,
    path: str,
    params: Dict[str, Any],
    timeout_sec: float = 15.0,
) -> Any:
    url = base_url.rstrip("/") + path
    resp = session.get(url, params=params, timeout=timeout_sec)
    resp.raise_for_status()
    return resp.json()


def resolve_category_ids(
    session: requests.Session,
    *,
    base_url: str,
    slugs: List[str],
) -> Tuple[List[int], List[str]]:
    """
    카테고리 slug 목록을 카테고리 ID 목록으로 변환합니다.
    반환: (ids, missing_slugs)
    """
    slugs = [s.strip() for s in slugs if s.strip()]
    if not slugs:
        return [], []

    data = _wp_get_json(
        session,
        base_url=base_url,
        path=WP_CATEGORIES_PATH,
        params={
            "per_page": 100,
            "slug": slugs,  # requests가 slug=notice&slug=pinup 형태로 인코딩
            "_fields": "id,slug,name",
        },
    )

    found: Dict[str, int] = {}
    for c in data:
        slug = str(c.get("slug", "")).strip()
        cid = c.get("id")
        if slug and isinstance(cid, int):
            found[slug] = cid

    missing = [s for s in slugs if s not in found]
    ids = sorted(set(found.values()))
    return ids, missing


def fetch_latest_post_cursor(
    session: requests.Session,
    *,
    base_url: str,
    category_ids: List[int],
) -> Optional[Tuple[datetime, int]]:
    """
    현재 시점의 최신 글(필터 적용)을 기준점(cursor)으로 가져옵니다.
    """
    params: Dict[str, Any] = {
        "per_page": 1,
        "page": 1,
        "orderby": "date",
        "order": "desc",
        "_fields": "id,date_gmt",
    }
    if category_ids:
        params["categories"] = ",".join(str(x) for x in category_ids)

    posts = _wp_get_json(session, base_url=base_url, path=WP_POSTS_PATH, params=params)
    if not posts:
        return None

    p = posts[0]
    pid = int(p["id"])
    dt = _parse_wp_date_gmt(str(p.get("date_gmt", "")).strip())
    return dt, pid


def fetch_new_posts(
    session: requests.Session,
    *,
    base_url: str,
    category_ids: List[int],
    cursor_dt: datetime,
    cursor_id: int,
    per_page: int,
    max_to_collect: int,
) -> List[Dict[str, Any]]:
    """
    (cursor_dt, cursor_id) 이후의 새 글을 오래된 것부터(max_to_collect개까지) 수집합니다.
    WordPress의 after 파라미터는 '엄격히 이후'이므로, 동일 초 단위 글 누락을 방지하려고 1초를 빼서 조회한 뒤
    (dt, id) 튜플 비교로 최종 필터링합니다.
    """
    per_page = max(1, min(int(per_page), 100))
    max_to_collect = max(0, int(max_to_collect))
    if max_to_collect == 0:
        return []

    # 동일 초 타임스탬프 방지: 1초 이전부터 조회 후, 앱에서 (dt,id)로 필터링
    after_dt = cursor_dt - timedelta(seconds=1)
    after_iso = _format_dt_z(after_dt)

    collected: List[Dict[str, Any]] = []
    page = 1

    while True:
        params: Dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "orderby": "date",
            "order": "asc",
            "after": after_iso,
            "_fields": "id,date_gmt,link,title",
        }
        if category_ids:
            params["categories"] = ",".join(str(x) for x in category_ids)

        try:
            posts = _wp_get_json(session, base_url=base_url, path=WP_POSTS_PATH, params=params)
        except requests.HTTPError as e:
            # WordPress REST API는 "존재하지 않는 페이지(page=2인데 총 1페이지만 존재)" 요청에 대해
            # 빈 배열이 아니라 400(rest_post_invalid_page_number)을 반환합니다.
            # 이는 정상적인 pagination 종료 조건이므로 조용히 루프를 끝냅니다.
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code == 400 and page > 1:
                try:
                    err = resp.json()
                    code = err.get("code")
                except Exception:
                    code = None
                if code in {None, "rest_post_invalid_page_number"}:
                    break
            raise
        if not posts:
            break

        for p in posts:
            pid = int(p.get("id", 0))
            dt = _parse_wp_date_gmt(str(p.get("date_gmt", "")).strip())
            if (dt, pid) > (cursor_dt, cursor_id):
                collected.append(p)
                if len(collected) >= max_to_collect:
                    return collected

        page += 1

    return collected


def send_slack_message(
    session: requests.Session,
    *,
    webhook_url: str,
    text: str,
    channel: Optional[str] = None,
    username: Optional[str] = None,
    timeout_sec: float = 15.0,
) -> None:
    payload: Dict[str, Any] = {
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if channel:
        payload["channel"] = channel
    if username:
        payload["username"] = username

    resp = session.post(webhook_url, json=payload, timeout=timeout_sec)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Slack webhook 실패: HTTP {resp.status_code} / body={resp.text[:300]!r}")


def fetch_latest_post(
    session: requests.Session,
    *,
    base_url: str,
    category_ids: List[int],
) -> Optional[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "per_page": 1,
        "page": 1,
        "orderby": "date",
        "order": "desc",
        "_fields": "id,date_gmt,link,title",
    }
    if category_ids:
        params["categories"] = ",".join(str(x) for x in category_ids)
    posts = _wp_get_json(session, base_url=base_url, path=WP_POSTS_PATH, params=params)
    if not posts:
        return None
    return posts[0]


def make_stream_key(base_url: str, category_ids: List[int]) -> str:
    base = base_url.rstrip("/")
    if category_ids:
        cats = ",".join(str(x) for x in sorted(set(category_ids)))
    else:
        cats = "all"
    return f"{base}{WP_POSTS_PATH}|categories={cats}"


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "streams": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(str(path) + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


@dataclass(frozen=True)
class Config:
    wp_base_url: str
    wp_watch_all: bool
    wp_category_ids: List[int]
    wp_category_slugs: List[str]
    wp_per_page: int
    max_notify_per_run: int
    send_on_first_run: bool
    state_path: Path

    slack_webhook_url: Optional[str]
    slack_channel: Optional[str]
    slack_username: Optional[str]

    alert_feed_name: str
    alert_emoji: str

    dry_run: bool
    init_only: bool
    test_latest: bool


def build_config_from_env_and_args(args: argparse.Namespace) -> Config:
    # dotenv 로드(사용자가 .env를 만든 경우)
    load_dotenv()

    wp_base_url = args.wp_base_url or os.getenv("WP_BASE_URL") or "https://architecture.snu.ac.kr"

    wp_watch_all = args.watch_all if args.watch_all is not None else _parse_bool(os.getenv("WP_WATCH_ALL"), default=False)

    env_cat_ids = _parse_csv_int(os.getenv("WP_CATEGORY_IDS"))
    env_cat_slugs = _parse_csv_str(os.getenv("WP_CATEGORY_SLUGS"))

    wp_category_ids = args.category_ids if args.category_ids is not None else env_cat_ids
    wp_category_slugs = args.category_slugs if args.category_slugs is not None else env_cat_slugs

    # 아무 것도 지정하지 않으면 "공지사항(notice)"을 기본으로 감시 (노이즈 최소화)
    if not wp_watch_all and not wp_category_ids and not wp_category_slugs:
        wp_category_slugs = ["notice"]

    wp_per_page = int(args.per_page or os.getenv("WP_PER_PAGE") or 30)
    max_notify_per_run = int(args.max_notify or os.getenv("MAX_NOTIFY_PER_RUN") or 20)
    send_on_first_run = _parse_bool(os.getenv("SEND_ON_FIRST_RUN"), default=False)
    if args.send_on_first_run is not None:
        send_on_first_run = args.send_on_first_run

    state_path = Path(args.state_path or os.getenv("STATE_PATH") or "./state.json")

    slack_webhook_url = args.slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    slack_channel = args.slack_channel or os.getenv("SLACK_CHANNEL")
    slack_username = args.slack_username or os.getenv("SLACK_USERNAME")

    alert_feed_name = os.getenv("ALERT_FEED_NAME") or "서울대 건축학과"
    alert_emoji = os.getenv("ALERT_EMOJI") or ":newspaper:"

    return Config(
        wp_base_url=wp_base_url,
        wp_watch_all=wp_watch_all,
        wp_category_ids=wp_category_ids,
        wp_category_slugs=wp_category_slugs,
        wp_per_page=wp_per_page,
        max_notify_per_run=max_notify_per_run,
        send_on_first_run=send_on_first_run,
        state_path=state_path,
        slack_webhook_url=slack_webhook_url,
        slack_channel=slack_channel,
        slack_username=slack_username,
        alert_feed_name=alert_feed_name,
        alert_emoji=alert_emoji,
        dry_run=bool(args.dry_run),
        init_only=bool(args.init),
        test_latest=bool(args.test_latest),
    )


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="architecture.snu.ac.kr 새 글 -> Slack 알림")
    p.add_argument("--init", action="store_true", help="최신 글을 기준점으로 저장만 하고 종료(초기화)")
    p.add_argument("--dry-run", action="store_true", help="Slack 전송 없이 콘솔에만 출력")
    p.add_argument("--test-latest", action="store_true", help="최신 글 1건을 [TEST]로 Slack에 전송(상태 저장 없음)")

    p.add_argument("--wp-base-url", default=None, help="기본: https://architecture.snu.ac.kr")
    p.add_argument("--watch-all", action="store_true", default=None, help="카테고리 필터 없이 전체 글 감시")
    p.add_argument("--category-ids", default=None, help="예: 20,33 (WP_CATEGORY_IDS 대체)")
    p.add_argument("--category-slugs", default=None, help="예: notice,pinup (WP_CATEGORY_SLUGS 대체)")
    p.add_argument("--per-page", default=None, help="WP_PER_PAGE 대체(1~100)")
    p.add_argument("--max-notify", default=None, help="MAX_NOTIFY_PER_RUN 대체")
    p.add_argument("--send-on-first-run", action="store_true", default=None, help="상태 파일이 없을 때도 알림 전송(주의)")
    p.add_argument("--state-path", default=None, help="STATE_PATH 대체(기본 ./state.json)")

    p.add_argument("--slack-webhook-url", default=None, help="SLACK_WEBHOOK_URL 대체")
    p.add_argument("--slack-channel", default=None, help="SLACK_CHANNEL 대체(웹훅 설정에 따라 무시될 수 있음)")
    p.add_argument("--slack-username", default=None, help="SLACK_USERNAME 대체")

    ns = p.parse_args(argv)

    if ns.category_ids is not None:
        ns.category_ids = _parse_csv_int(ns.category_ids)
    if ns.category_slugs is not None:
        ns.category_slugs = _parse_csv_str(ns.category_slugs)

    return ns


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(msg)


def run(cfg: Config) -> int:
    session = _requests_session()

    # 카테고리 해석
    category_ids: List[int] = []
    if not cfg.wp_watch_all:
        category_ids = list(cfg.wp_category_ids)
        resolved_ids, missing = resolve_category_ids(
            session,
            base_url=cfg.wp_base_url,
            slugs=cfg.wp_category_slugs,
        )
        category_ids = sorted(set(category_ids) | set(resolved_ids))
        if missing:
            print(f"[WARN] 존재하지 않는 카테고리 slug: {missing}", file=sys.stderr)

    stream_key = make_stream_key(cfg.wp_base_url, category_ids)
    state = load_state(cfg.state_path)
    streams = state.setdefault("streams", {})
    stream = streams.get(stream_key)

    # 테스트 모드: 상태 저장 없이 최신 글 1건을 [TEST]로 전송
    if cfg.test_latest:
        latest = fetch_latest_post(session, base_url=cfg.wp_base_url, category_ids=category_ids)
        if latest is None:
            text = "[TEST] architecture.snu.ac.kr 최신 글 조회 결과: 게시물이 없습니다."
            if cfg.dry_run:
                print(text)
                return 0
            _require(bool(cfg.slack_webhook_url), "SLACK_WEBHOOK_URL이 필요합니다. (dry-run이면 필요 없음)")
            send_slack_message(session, webhook_url=str(cfg.slack_webhook_url), text=text, channel=cfg.slack_channel, username=cfg.slack_username)
            return 0

        text = build_slack_summary_text(
            posts=[latest],
            feed_name=cfg.alert_feed_name,
            emoji=cfg.alert_emoji,
            is_test=True,
        )

        if cfg.dry_run:
            print(text)
            return 0

        _require(bool(cfg.slack_webhook_url), "SLACK_WEBHOOK_URL이 필요합니다. (dry-run이면 필요 없음)")
        send_slack_message(
            session,
            webhook_url=str(cfg.slack_webhook_url),
            text=text,
            channel=cfg.slack_channel,
            username=cfg.slack_username,
        )
        return 0

    # init 모드: 항상 최신 글 기준점으로 저장
    if cfg.init_only:
        cursor = fetch_latest_post_cursor(session, base_url=cfg.wp_base_url, category_ids=category_ids)
        if cursor is None:
            print("[INFO] 최신 글을 찾지 못했습니다. (게시물이 없을 수 있음)")
            return 0
        cursor_dt, cursor_id = cursor
        streams[stream_key] = {
            "cursor": {"date_gmt": _format_dt_z(cursor_dt), "id": cursor_id},
            "category_ids": category_ids,
            "updated_at": _utc_now_iso(),
        }
        save_state(cfg.state_path, state)
        print(f"[OK] 초기화 완료: cursor=({streams[stream_key]['cursor']['date_gmt']}, {cursor_id})")
        return 0

    # 최초 실행(상태 없음) 처리
    if stream is None:
        if not cfg.send_on_first_run:
            cursor = fetch_latest_post_cursor(session, base_url=cfg.wp_base_url, category_ids=category_ids)
            if cursor is None:
                print("[INFO] 기준점으로 삼을 최신 글이 없습니다.")
                streams[stream_key] = {
                    "cursor": {"date_gmt": _format_dt_z(datetime(1970, 1, 1, tzinfo=timezone.utc)), "id": 0},
                    "category_ids": category_ids,
                    "updated_at": _utc_now_iso(),
                }
            else:
                cursor_dt, cursor_id = cursor
                streams[stream_key] = {
                    "cursor": {"date_gmt": _format_dt_z(cursor_dt), "id": cursor_id},
                    "category_ids": category_ids,
                    "updated_at": _utc_now_iso(),
                }
            save_state(cfg.state_path, state)
            print("[OK] 최초 실행: 스팸 방지를 위해 기준점만 저장했습니다. 이후부터 새 글만 알림됩니다.")
            print(f"      state: {cfg.state_path}")
            return 0

        # send_on_first_run=True: 최근 max_notify_per_run개만 전송하고 그 지점으로 커서 저장(안전/효율)
        _require(cfg.max_notify_per_run > 0, "MAX_NOTIFY_PER_RUN이 0이면 최초 전송 모드가 의미가 없습니다.")
        params: Dict[str, Any] = {
            "per_page": max(1, min(cfg.max_notify_per_run, 100)),
            "page": 1,
            "orderby": "date",
            "order": "desc",
            "_fields": "id,date_gmt,link,title",
        }
        if category_ids:
            params["categories"] = ",".join(str(x) for x in category_ids)
        posts = _wp_get_json(session, base_url=cfg.wp_base_url, path=WP_POSTS_PATH, params=params)
        posts = list(reversed(posts))  # 오래된 것부터

        if not posts:
            print("[INFO] 전송할 글이 없습니다.")
            streams[stream_key] = {
                "cursor": {"date_gmt": _format_dt_z(datetime(1970, 1, 1, tzinfo=timezone.utc)), "id": 0},
                "category_ids": category_ids,
                "updated_at": _utc_now_iso(),
            }
            save_state(cfg.state_path, state)
            return 0

        if not cfg.dry_run:
            _require(bool(cfg.slack_webhook_url), "SLACK_WEBHOOK_URL이 필요합니다. (dry-run이면 필요 없음)")

        for p in posts:
            pass  # (기존 개별 전송 로직 제거: 아래에서 1개 메시지로 요약 전송)

        text = build_slack_summary_text(
            posts=posts,
            feed_name=cfg.alert_feed_name,
            emoji=cfg.alert_emoji,
            is_test=False,
        )
        if cfg.dry_run:
            print(text)
        else:
            send_slack_message(
                session,
                webhook_url=str(cfg.slack_webhook_url),
                text=text,
                channel=cfg.slack_channel,
                username=cfg.slack_username,
            )

        last = posts[-1]
        last_dt = _parse_wp_date_gmt(str(last.get("date_gmt", "")).strip())
        last_id = int(last.get("id", 0))
        streams[stream_key] = {
            "cursor": {"date_gmt": _format_dt_z(last_dt), "id": last_id},
            "category_ids": category_ids,
            "updated_at": _utc_now_iso(),
        }
        save_state(cfg.state_path, state)
        print(f"[OK] 최초 전송 완료: cursor=({streams[stream_key]['cursor']['date_gmt']}, {last_id})")
        return 0

    # 정상 실행(상태 존재)
    cursor_date = str(stream.get("cursor", {}).get("date_gmt", "1970-01-01T00:00:00Z"))
    cursor_id = int(stream.get("cursor", {}).get("id", 0))
    cursor_dt = _parse_wp_date_gmt(cursor_date)

    posts = fetch_new_posts(
        session,
        base_url=cfg.wp_base_url,
        category_ids=category_ids,
        cursor_dt=cursor_dt,
        cursor_id=cursor_id,
        per_page=cfg.wp_per_page,
        max_to_collect=cfg.max_notify_per_run,
    )

    if not posts:
        # 상태 갱신(선택): last_checked를 남기고 싶으면 여기에 추가 가능
        return 0

    if not cfg.dry_run:
        _require(bool(cfg.slack_webhook_url), "SLACK_WEBHOOK_URL이 필요합니다. (dry-run이면 필요 없음)")

    text = build_slack_summary_text(
        posts=posts,
        feed_name=cfg.alert_feed_name,
        emoji=cfg.alert_emoji,
        is_test=False,
    )
    if cfg.dry_run:
        print(text)
    else:
        send_slack_message(
            session,
            webhook_url=str(cfg.slack_webhook_url),
            text=text,
            channel=cfg.slack_channel,
            username=cfg.slack_username,
        )

    last = posts[-1]
    last_dt = _parse_wp_date_gmt(str(last.get("date_gmt", "")).strip())
    last_id = int(last.get("id", 0))
    stream["cursor"] = {"date_gmt": _format_dt_z(last_dt), "id": last_id}
    stream["category_ids"] = category_ids
    stream["updated_at"] = _utc_now_iso()
    streams[stream_key] = stream
    save_state(cfg.state_path, state)

    return 0


def main() -> None:
    try:
        args = parse_args(sys.argv[1:])
        cfg = build_config_from_env_and_args(args)
        raise SystemExit(run(cfg))
    except requests.HTTPError as e:
        print(f"[ERROR] HTTP 오류: {e}", file=sys.stderr)
        raise SystemExit(2)
    except requests.RequestException as e:
        print(f"[ERROR] 네트워크 오류: {e}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as e:
        print(f"[ERROR] 예외: {e}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()


