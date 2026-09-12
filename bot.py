#!/usr/bin/env python3
"""
architecture.snu.ac.kr (SNU DAAE) 새 공지를 감지해 Slack/Notion으로 알림을 보내는 폴링 스크립트입니다.

2026년 사이트 개편 안내
- 기존 사이트는 WordPress였고 이 봇은 WordPress REST API(/wp-json/wp/v2/posts)를 사용했습니다.
- 사이트가 SPA(Single Page App)로 전면 재구축되면서 /wp-json API가 사라졌고,
  새 공지 데이터는 아래 JSON API로 제공됩니다.
      POST https://architecture.snu.ac.kr/rest/activities/getNotices
      body: {"page": 1}
      resp: {"err": 0, "list": [{"id", "category", "ctype", "title", "post_date"}, ...]}
  글 링크는 https://architecture.snu.ac.kr/post/{id} 형식입니다.
- post_date는 'YYYY.MM.DD'(날짜만, 시간 없음)이고 id는 날짜순으로 단조증가하지 않으므로,
  중복 알림 방지는 "이미 본 글 id 집합(seen_ids)"으로 처리합니다.

실행 예시
  1) 최초 기준점만 저장(스팸 방지): python3 bot.py --init
  2) 평상시 실행(새 글 있으면 전송): python3 bot.py
  3) 전송 없이 출력만(dry-run):     python3 bot.py --dry-run
  4) 최신 글 1건 테스트 전송:        python3 bot.py --test-latest
  5) Slack 연결 확인:               python3 bot.py --ping
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

try:
    from notion_client import Client
except ImportError:
    Client = None  # type: ignore[assignment, misc]


# 새 SPA 사이트의 공지 API. 같은 origin의 상대경로로 호출됩니다.
NOTICES_PATH = "/rest/activities/getNotices"
# 글 상세 페이지(SPA 클라이언트 라우트). 링크 생성에 사용합니다.
POST_PATH = "/post"

# seen_ids가 무한정 커지지 않도록 보관 상한(가장 큰 id 기준 최신 N개만 유지).
MAX_SEEN_IDS = 2000

# 페이지네이션 안전 상한. 오작동하거나 무한히 페이지를 내려주는 API가 있어도
# fetch_all_new_notices가 무한 루프에 빠지지 않도록 하는 하드 캡입니다.
MAX_FETCH_PAGES = 10


def _parse_bool(value: Optional[str], *, default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _clean_text(text: str) -> str:
    """제목에 HTML 엔티티/태그가 섞여 있을 수 있어 보기 좋게 정리합니다."""
    t = html.unescape(text or "")
    t = re.sub(r"<[^>]+>", "", t)  # 매우 단순한 tag strip
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slack_escape(text: str) -> str:
    """Slack mrkdwn 링크 텍스트에서 깨질 수 있는 문자들을 최소한으로 이스케이프합니다."""
    t = text or ""
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = t.replace("|", "｜")  # 링크 텍스트 구분자 충돌 방지
    return t


def _format_post_date(post_date: str) -> str:
    """'2026.06.08' -> '2026. 6. 8.' (한국어 날짜 표기). 파싱 실패 시 원문 그대로."""
    s = (post_date or "").strip()
    m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", s)
    if not m:
        return s
    y, mo, d = (int(g) for g in m.groups())
    return f"{y}. {mo}. {d}."


_KST = timezone(timedelta(hours=9))


# ──────────────────────────────────────────────────────────────────────────
# 공지(post) 데이터 정규화
# ──────────────────────────────────────────────────────────────────────────
def _post_link(base_url: str, post_id: int) -> str:
    return f"{base_url.rstrip('/')}{POST_PATH}/{post_id}"


def normalize_notice(item: Dict[str, Any], *, base_url: str) -> Dict[str, Any]:
    """
    getNotices의 한 항목을 메시지 빌더가 쓰기 좋은 공통 형태로 변환합니다.
    반환 키: id(int), title(str), link(str), post_date(str 'YYYY.MM.DD'), ctype(str)
    """
    pid = int(item.get("id", 0))
    return {
        "id": pid,
        "title": _clean_text(str(item.get("title", ""))),
        "link": _post_link(base_url, pid) if pid else "",
        "post_date": str(item.get("post_date", "")).strip(),
        "ctype": str(item.get("ctype", "")).strip(),
    }


def build_slack_summary_text(
    *,
    posts: List[Dict[str, Any]],
    feed_name: str,
    emoji: str,
    is_test: bool,
) -> str:
    """
    fallback / 알림 텍스트. 1개의 메시지에 N개 글을 요약합니다.
      :신문: *건축학과* 새 글 1개
      - 제목  (2026. 6. 8.)
    """
    count = len(posts)
    display_name = _slack_escape((feed_name or "").strip())
    display_name = display_name.replace("건축학과", "*건축학과*")
    header = f"{emoji} {display_name} 새 글 {count}개"

    lines = [header]
    for p in posts:
        title = _clean_text(str(p.get("title", "")))
        link = str(p.get("link", "")).strip()
        post_date = str(p.get("post_date", "")).strip()

        safe_title = _slack_escape(title or "(제목 없음)")
        title_md = f"<{link}|{safe_title}>" if link else safe_title
        date_str = _format_post_date(post_date)

        if date_str:
            lines.append(f"- {title_md}  ({date_str})")
        else:
            lines.append(f"- {title_md}")

    return "\n".join(lines)


def build_slack_attachments(
    *,
    posts: List[Dict[str, Any]],
    feed_name: str,
    emoji: str,
    is_test: bool,
    base_url: str = "",
) -> List[Dict[str, Any]]:
    """
    Slack Attachment + Block Kit으로 프리미엄 알림 메시지를 구성합니다.
    네이비 컬러바(#003876) + 인용 스타일 제목 + Primary 버튼 + 브랜딩 푸터.
    """
    count = len(posts)

    header = f"{emoji}  *{feed_name} 새 공지사항*"
    if is_test:
        header = f"[TEST]  {header}"
    subtitle = f"서울대학교 {feed_name}  ｜  🔔 {count}건의 새로운 공지사항"

    blocks: List[Dict[str, Any]] = []

    # ── Header + Subtitle ──
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"{header}\n{subtitle}"},
    })
    blocks.append({"type": "divider"})

    # ── Posts (인용 스타일 + Primary 버튼) ──
    for idx, p in enumerate(posts):
        title = _clean_text(str(p.get("title", "")))
        link = str(p.get("link", "")).strip()
        post_date = str(p.get("post_date", "")).strip()
        ctype = str(p.get("ctype", "")).strip()

        safe_title = _slack_escape(title or "(제목 없음)")

        if link:
            title_text = f"> *<{link}|{safe_title}>*"
        else:
            title_text = f"> *{safe_title}*"

        # 분류/날짜 메타 라인
        meta_bits = []
        if ctype:
            meta_bits.append(f"`{_slack_escape(ctype)}`")
        date_str = _format_post_date(post_date)
        if date_str:
            meta_bits.append(f"📅 {date_str}")
        if meta_bits:
            title_text += "\n> " + "  ｜  ".join(meta_bits)

        section: Dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": title_text},
        }

        if link:
            section["accessory"] = {
                "type": "button",
                "text": {"type": "plain_text", "text": "확인하기", "emoji": True},
                "url": link,
                "style": "primary",
                "action_id": f"open_post_{idx}",
            }

        blocks.append(section)

    blocks.append({"type": "divider"})

    # ── 전체 공지사항 보기 버튼 ──
    if base_url:
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "📋  전체 공지사항 보기", "emoji": True},
                "url": base_url.rstrip("/") + "/posts/notice",
                "action_id": "view_all_posts",
            }],
        })

    # ── 브랜딩 푸터 ──
    now_kst = datetime.now(_KST)
    ts = f"{now_kst.year}. {now_kst.month:02d}. {now_kst.day:02d}  {now_kst.strftime('%H:%M')} KST"
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"🏫 {feed_name} Notice Bot  ｜  {ts}"}],
    })

    return [{"color": "#003876", "blocks": blocks}]


def build_ping_attachments(*, feed_name: str, base_url: str = "") -> List[Dict[str, Any]]:
    """Slack 연결 확인용 ping 메시지. 그린 컬러바(#2eb67d)."""
    now_kst = datetime.now(_KST)
    ts = f"{now_kst.year}. {now_kst.month:02d}. {now_kst.day:02d}  {now_kst.strftime('%H:%M')} KST"

    site_domain = (
        base_url.replace("https://", "").replace("http://", "").rstrip("/")
        if base_url else ""
    )
    status_line = f"✅  *연결 상태 정상*\n{feed_name} 알림 봇이 정상적으로 작동 중입니다."
    if site_domain:
        status_line += f"\n🔗  대상 사이트: {site_domain}"

    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": status_line},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"🏫 {feed_name} Notice Bot  ｜  {ts}"}],
        },
    ]

    return [{"color": "#2eb67d", "blocks": blocks}]


# 실패 알림 본문 길이 상한(거대한 스택트레이스/응답이 Slack 페이로드를 폭주시키지 않도록).
MAX_ERROR_SUMMARY_CHARS = 500


def build_heartbeat_attachments(*, feed_name: str, base_url: str = "") -> List[Dict[str, Any]]:
    """정기 생존 신호(heartbeat)용 메시지. ping과 동일한 그린 컬러바(#2eb67d)."""
    now_kst = datetime.now(_KST)
    ts = f"{now_kst.year}. {now_kst.month:02d}. {now_kst.day:02d}  {now_kst.strftime('%H:%M')} KST"

    site_domain = (
        base_url.replace("https://", "").replace("http://", "").rstrip("/")
        if base_url else ""
    )
    status_line = f"💓  *정상 동작 중*\n{feed_name} 알림 봇이 예정대로 실행되고 있습니다."
    if site_domain:
        status_line += f"\n🔗  대상 사이트: {site_domain}"

    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": status_line},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"🏫 {feed_name} Notice Bot  ｜  {ts}"}],
        },
    ]

    return [{"color": "#2eb67d", "blocks": blocks}]


def build_failure_attachments(
    *,
    feed_name: str,
    error_summary: str,
    base_url: str = "",
) -> List[Dict[str, Any]]:
    """
    실행 실패를 눈에 띄게 알리는 메시지. 레드 컬러바(#e01e5a).
    error_summary는 과도한 페이로드 방지를 위해 앞부분만 사용하며,
    환경변수/시크릿 값은 절대 여기에 넣지 않습니다(호출부에서 예외 메시지만 전달).
    """
    summary = (error_summary or "").strip()
    if len(summary) > MAX_ERROR_SUMMARY_CHARS:
        summary = summary[:MAX_ERROR_SUMMARY_CHARS] + "…(생략)"
    if not summary:
        summary = "(원인 정보 없음)"

    now_kst = datetime.now(_KST)
    ts = f"{now_kst.year}. {now_kst.month:02d}. {now_kst.day:02d}  {now_kst.strftime('%H:%M')} KST"

    status_line = (
        f"🚨  *알림 봇 실행 실패*\n{feed_name} 공지 알림을 처리하는 중 오류가 발생했습니다.\n"
        f"```{summary}```"
    )

    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": status_line},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"🏫 {feed_name} Notice Bot  ｜  {ts}"}],
        },
    ]

    return [{"color": "#e01e5a", "blocks": blocks}]


def _requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "daae-slack-bot/2.0 (+https://architecture.snu.ac.kr)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    # architecture.snu.ac.kr는 여러 IP로 라운드로빈되며 일부 IP(개편 test 서버)는
    # 외부 접속을 거부할 수 있습니다. 연결 실패 시 재시도하면 다른 IP로 재연결됩니다.
    retry = Retry(
        total=4,
        connect=4,
        read=2,
        backoff_factor=0.6,
        status_forcelist=[502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch_notices(
    session: requests.Session,
    *,
    base_url: str,
    page: int = 1,
    timeout_sec: float = 15.0,
) -> List[Dict[str, Any]]:
    """
    POST /rest/activities/getNotices 를 호출해 공지 목록(raw)을 반환합니다.
    응답: {"err": 0, "list": [...]}  (list가 없으면 빈 리스트)
    """
    url = base_url.rstrip("/") + NOTICES_PATH
    resp = session.post(url, json={"page": page}, timeout=timeout_sec)
    resp.raise_for_status()

    # 이 API는 JSON 본문을 보내면서도 Content-Type을 text/html로 응답하므로
    # 헤더가 아니라 '본문'으로 판별합니다. SPA fallback(index.html)이면 본문이 '<'로 시작합니다.
    body = resp.text or ""
    if body.lstrip().startswith("<"):
        snippet = body[:200].replace("\n", " ")
        raise RuntimeError(
            "공지 API가 JSON 대신 HTML(SPA 페이지)을 반환했습니다. "
            f"API 경로/도메인이 바뀌었을 수 있습니다. 응답 일부: {snippet!r}"
        )
    try:
        data = resp.json()
    except ValueError as e:
        snippet = body[:200].replace("\n", " ")
        raise RuntimeError(f"공지 API 응답을 JSON으로 파싱하지 못했습니다: {e} / 응답 일부: {snippet!r}") from e
    items = data.get("list")
    if not isinstance(items, list):
        raise RuntimeError(f"공지 API 응답에 'list'가 없습니다: {str(data)[:200]!r}")
    return items


def fetch_all_new_notices(
    session: requests.Session,
    *,
    base_url: str,
    seen_ids: "set[int]",
    max_pages: int = MAX_FETCH_PAGES,
    timeout_sec: float = 15.0,
) -> "tuple[List[Dict[str, Any]], List[int]]":
    """
    다운타임/버스트 이후 2페이지 이상 쌓인 새 공지를 놓치지 않도록,
    page=1부터 시작해 새 글이 더 이상 없을 때까지 페이지를 이어서 조회합니다.

    각 페이지에 대해:
      - 기존 fetch_notices를 그대로 호출(HTML drift / JSON 파싱 실패 시
        RuntimeError를 그대로 전파),
      - normalize_notice로 정규화하고 id>0 항목만 취합,
      - 지금까지 모은 정규화 공지(notices)와 current_ids(전 페이지 합집합)에 누적합니다.

    다음 중 하나가 만족되면 페이지 조회를 멈춥니다.
      1) raw 페이지가 비어 있음(더 이상 글이 없음),
      2) 해당 페이지가 '새 글'을 하나도 기여하지 못함
         (= 이 페이지의 id가 전부 이미 본 글(seen_ids)이거나 앞 페이지에서 이미
            취합한 글(중복 고정 공지 등)뿐이므로, 더 깊은 페이지는 더 오래된 글이라
            역시 이미 봤다고 간주),
      3) max_pages에 도달(안전 상한).

    가정: 이 API는 최신 글이 앞 페이지에 오는 안정적인 정렬을 유지합니다.
    id는 단조증가하지 않고 1페이지에 고정(pinned) 공지가 섞일 수 있으므로,
    '이 페이지가 기여한 새 글'은 seen_ids에도 없고 앞 페이지에서 이미 취합하지도
    않은 id로만 계산합니다. 덕분에 이미 취합한 고정 공지가 다시 실린 페이지 때문에
    불필요하게 한 페이지를 더 조회하는 일이 없습니다.
    어떤 경우에도 max_pages 안전 상한이 종료를 보장합니다.

    ※ 정렬 가정이 깨지면(최신 글이 뒤 페이지로 오는 등) 조건 2가 실제보다 일찍
    참이 되어 오래됐지만 아직 안 본 글을 놓칠 수 있습니다. 이 봇이 다시 백로그를
    놓치기 시작하면 '업스트림 API의 페이지 정렬이 바뀌었는지'를 가장 먼저 확인하세요.

    반환: (조회한 모든 페이지의 정규화 공지 리스트, 전 페이지 current_ids 합집합)
    """
    cap = max(1, int(max_pages))
    all_notices: List[Dict[str, Any]] = []
    all_current_ids: List[int] = []
    accumulated_ids: "set[int]" = set()  # 페이지 간 중복(고정 공지 등) 제거용

    for page in range(1, cap + 1):
        raw_items = fetch_notices(session, base_url=base_url, page=page, timeout_sec=timeout_sec)
        if not raw_items:
            # 더 이상 글이 없는 빈 페이지 → 종료.
            break

        page_notices = [
            normalize_notice(it, base_url=base_url)
            for it in raw_items
            if int(it.get("id", 0)) > 0
        ]
        page_ids = [p["id"] for p in page_notices]

        # 페이지 간에 같은 글(예: 고정 공지)이 중복될 수 있으므로 id 기준으로 한 번만 취합.
        # 이 페이지가 새로 추가한 id(앞 페이지에 없던 것)를 따로 모아 종료 판정에 씁니다.
        newly_added_ids: "set[int]" = set()
        for p in page_notices:
            if p["id"] not in accumulated_ids:
                accumulated_ids.add(p["id"])
                all_notices.append(p)
                all_current_ids.append(p["id"])
                newly_added_ids.add(p["id"])

        # 이 페이지가 '새 글'을 실제로 기여했을 때만 다음 페이지를 조회합니다.
        # '기여한 새 글'은 seen_ids에 없으면서(=아직 못 본) 앞선 페이지에서 이미
        # 취합하지도 않은(=accumulated_ids에 없던) id입니다.
        # accumulated_ids는 이 페이지의 항목까지 이미 반영된 상태이므로, 그 차집합을
        # 다시 계산하는 대신 위 취합 루프에서 이 페이지가 새로 추가한 id가 있었는지로 판정합니다.
        # 이렇게 하면 page_ids만 보던 기존 방식과 달리, 앞 페이지에서 이미 모은 고정(pinned)
        # 공지가 아직 seen_ids에 없다는 이유만으로 다음 페이지를 한 번 더 조회하는 낭비를 없앱니다.
        contributed_new = any(
            pid in newly_added_ids and pid not in seen_ids for pid in page_ids
        )
        if not contributed_new:
            break

    return all_notices, all_current_ids


def send_slack_message(
    session: requests.Session,
    *,
    webhook_url: str,
    text: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    channel: Optional[str] = None,
    username: Optional[str] = None,
    timeout_sec: float = 15.0,
) -> None:
    payload: Dict[str, Any] = {
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if attachments:
        payload["attachments"] = attachments
    if channel:
        payload["channel"] = channel
    if username:
        payload["username"] = username

    resp = session.post(webhook_url, json=payload, timeout=timeout_sec)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Slack webhook 실패: HTTP {resp.status_code} / body={resp.text[:300]!r}")


def send_failure_notification(
    session: requests.Session,
    *,
    cfg: "Config",
    error_summary: str,
) -> None:
    """
    실행 실패 시 Slack으로 실패 알림을 보냅니다(best-effort).
    이 함수 자체가 다시 실패하더라도 원래의 오류를 가리지 않도록,
    내부에서 예외를 잡아 stderr로만 남기고 조용히 반환합니다.
    """
    if not cfg.slack_webhook_url:
        return
    try:
        text = f"[ERROR] {cfg.alert_feed_name} 알림 봇 실행 실패"
        attachments = build_failure_attachments(
            feed_name=cfg.alert_feed_name,
            error_summary=error_summary,
            base_url=cfg.base_url,
        )
        send_slack_message(
            session,
            webhook_url=str(cfg.slack_webhook_url),
            text=text,
            attachments=attachments,
            channel=cfg.slack_channel,
            username=cfg.slack_username,
        )
    except Exception as e:  # noqa: BLE001 - best-effort, must not mask original error
        print(f"[WARN] 실패 알림 전송에 실패했습니다(원래 오류는 별도 출력됨): {e}", file=sys.stderr)


def _normalize_notion_page_id(page_id_or_url: str) -> str:
    """Notion 페이지 ID 정규화(URL이면 마지막 32자 hex 추출, 아니면 하이픈 제거)."""
    s = (page_id_or_url or "").strip()
    if not s:
        return ""
    if "notion.so" in s:
        parts = s.split("-")
        if parts:
            page_id = parts[-1]
            if len(page_id) == 32 and all(c in "0123456789abcdef" for c in page_id.lower()):
                return page_id
    s = s.replace("-", "")
    if len(s) == 32 and all(c in "0123456789abcdef" for c in s.lower()):
        return s
    return s


def send_to_notion(
    *,
    token: str,
    page_id: str,
    posts: List[Dict[str, Any]],
    feed_name: str,
    emoji: str,
    is_test: bool,
    dry_run: bool = False,
) -> None:
    """Notion 페이지에 새 글 목록을 callout 블록으로 추가합니다."""
    if Client is None:
        raise RuntimeError("notion-client 라이브러리가 설치되지 않았습니다. pip install notion-client")

    if dry_run:
        print(f"[DRY-RUN] Notion 전송: {len(posts)}개 글")
        return

    normalized_page_id = _normalize_notion_page_id(page_id)
    if not normalized_page_id:
        raise ValueError(f"유효하지 않은 Notion 페이지 ID: {page_id}")

    client = Client(auth=token)

    blocks: List[Dict[str, Any]] = []

    header_text = f"{emoji} {feed_name} 새 글 {len(posts)}개"
    if is_test:
        header_text = f"[TEST] {header_text}"

    blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": header_text}}]},
    })
    blocks.append({"object": "block", "type": "divider", "divider": {}})

    for idx, p in enumerate(posts):
        title = _clean_text(str(p.get("title", ""))) or "(제목 없음)"
        link = str(p.get("link", "")).strip()
        date_str = _format_post_date(str(p.get("post_date", "")).strip())

        rich_text_parts: List[Dict[str, Any]] = [
            {"type": "text", "text": {"content": title}, "annotations": {"bold": True}}
        ]
        if date_str:
            rich_text_parts.append({
                "type": "text",
                "text": {"content": f"\n📅 {date_str}"},
                "annotations": {"bold": False},
            })
        if link:
            rich_text_parts.append({
                "type": "text",
                "text": {"content": "\n🔗 "},
                "annotations": {"bold": False},
            })
            rich_text_parts.append({
                "type": "text",
                "text": {"content": "원문 보기", "link": {"url": link}},
                "annotations": {"bold": False},
            })

        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": rich_text_parts,
                "icon": {"emoji": "📰"},
                "color": "blue",
            },
        })
        if idx < len(posts) - 1:
            blocks.append({"object": "block", "type": "divider", "divider": {}})

    try:
        client.blocks.children.append(block_id=normalized_page_id, children=blocks)
        print(f"[OK] Notion 전송 완료: {len(posts)}개 글을 페이지에 추가했습니다.")
    except Exception as e:
        error_msg = str(e)
        print(f"[ERROR] Notion API 실패: {error_msg}", file=sys.stderr)
        raise RuntimeError(f"Notion API 실패: {error_msg}") from e


def make_stream_key(base_url: str) -> str:
    return f"{base_url.rstrip('/')}{NOTICES_PATH}"


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 2, "streams": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(str(path) + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _prune_seen_ids(seen_ids: List[int]) -> List[int]:
    """가장 큰 id 기준 최신 MAX_SEEN_IDS개만 유지(상태 파일 비대화 방지)."""
    uniq = sorted(set(int(x) for x in seen_ids), reverse=True)
    return uniq[:MAX_SEEN_IDS]


@dataclass(frozen=True)
class Config:
    base_url: str
    max_notify_per_run: int
    send_on_first_run: bool
    state_path: Path

    slack_webhook_url: Optional[str]
    slack_channel: Optional[str]
    slack_username: Optional[str]

    notion_token: Optional[str]
    notion_page_id: Optional[str]

    alert_feed_name: str
    alert_emoji: str

    dry_run: bool
    init_only: bool
    test_latest: bool
    ping: bool
    heartbeat: bool


def build_config_from_env_and_args(args: argparse.Namespace) -> Config:
    load_dotenv()

    # 하위호환: 기존 WP_BASE_URL 환경변수도 그대로 인식합니다.
    base_url = (
        args.base_url
        or os.getenv("BASE_URL")
        or os.getenv("WP_BASE_URL")
        or "https://architecture.snu.ac.kr"
    )

    max_notify_per_run = int(args.max_notify or os.getenv("MAX_NOTIFY_PER_RUN") or 20)
    send_on_first_run = _parse_bool(os.getenv("SEND_ON_FIRST_RUN"), default=False)
    if args.send_on_first_run is not None:
        send_on_first_run = args.send_on_first_run

    state_path = Path(args.state_path or os.getenv("STATE_PATH") or "./state.json")

    slack_webhook_url = args.slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    slack_channel = args.slack_channel or os.getenv("SLACK_CHANNEL")
    slack_username = args.slack_username or os.getenv("SLACK_USERNAME")

    notion_token = os.getenv("NOTION_TOKEN")
    notion_page_id = os.getenv("NOTION_PAGE_ID")

    alert_feed_name = os.getenv("ALERT_FEED_NAME") or "건축학과"
    alert_emoji = os.getenv("ALERT_EMOJI") or "📰"

    return Config(
        base_url=base_url,
        max_notify_per_run=max_notify_per_run,
        send_on_first_run=send_on_first_run,
        state_path=state_path,
        slack_webhook_url=slack_webhook_url,
        slack_channel=slack_channel,
        slack_username=slack_username,
        notion_token=notion_token,
        notion_page_id=notion_page_id,
        alert_feed_name=alert_feed_name,
        alert_emoji=alert_emoji,
        dry_run=bool(args.dry_run),
        init_only=bool(args.init),
        test_latest=bool(args.test_latest),
        ping=bool(args.ping),
        heartbeat=bool(args.heartbeat),
    )


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="architecture.snu.ac.kr(SNU DAAE) 새 공지 -> Slack/Notion 알림")
    p.add_argument("--init", action="store_true", help="현재 공지를 기준점으로 저장만 하고 종료(초기화)")
    p.add_argument("--dry-run", action="store_true", help="전송 없이 콘솔에만 출력")
    p.add_argument("--test-latest", action="store_true", help="최신 글 1건을 테스트 전송(상태 저장 없음)")
    p.add_argument("--ping", action="store_true", help="Slack 연결 테스트 메시지 전송")
    p.add_argument("--heartbeat", action="store_true", help="정기 생존 신호(정상 동작 중) 메시지 전송(상태 저장 없음)")

    p.add_argument("--base-url", default=None, help="기본: https://architecture.snu.ac.kr")
    p.add_argument("--max-notify", default=None, help="MAX_NOTIFY_PER_RUN 대체(한 번에 보낼 최대 글 수)")
    p.add_argument("--send-on-first-run", action="store_true", default=None, help="상태 파일이 없을 때도 알림 전송(주의)")
    p.add_argument("--state-path", default=None, help="STATE_PATH 대체(기본 ./state.json)")

    p.add_argument("--slack-webhook-url", default=None, help="SLACK_WEBHOOK_URL 대체")
    p.add_argument("--slack-channel", default=None, help="SLACK_CHANNEL 대체")
    p.add_argument("--slack-username", default=None, help="SLACK_USERNAME 대체")

    return p.parse_args(argv)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(msg)


def run(cfg: Config) -> int:
    # dry-run 경로는 네트워크 세션이 필요 없으므로, 실제 전송이 필요한 시점에만 세션을 만듭니다.
    _session_holder: List[requests.Session] = []

    def get_session() -> requests.Session:
        if not _session_holder:
            _session_holder.append(_requests_session())
        return _session_holder[0]

    # ── Ping 모드 ──
    if cfg.ping:
        text = f"[PING] {cfg.alert_feed_name} 알림 봇 연결 테스트"
        attachments = build_ping_attachments(feed_name=cfg.alert_feed_name, base_url=cfg.base_url)
        if cfg.dry_run:
            print(text)
            return 0
        _require(bool(cfg.slack_webhook_url), "SLACK_WEBHOOK_URL이 필요합니다.")
        send_slack_message(
            get_session(),
            webhook_url=str(cfg.slack_webhook_url),
            text=text,
            attachments=attachments,
            channel=cfg.slack_channel,
            username=cfg.slack_username,
        )
        print("[OK] Ping 전송 완료")
        return 0

    # ── Heartbeat 모드(정기 생존 신호) ──
    if cfg.heartbeat:
        text = f"[HEARTBEAT] {cfg.alert_feed_name} 알림 봇 정상 동작 중"
        attachments = build_heartbeat_attachments(feed_name=cfg.alert_feed_name, base_url=cfg.base_url)
        if cfg.dry_run:
            print(text)
            return 0
        _require(bool(cfg.slack_webhook_url), "SLACK_WEBHOOK_URL이 필요합니다.")
        send_slack_message(
            get_session(),
            webhook_url=str(cfg.slack_webhook_url),
            text=text,
            attachments=attachments,
            channel=cfg.slack_channel,
            username=cfg.slack_username,
        )
        print("[OK] Heartbeat 전송 완료")
        return 0

    stream_key = make_stream_key(cfg.base_url)
    state = load_state(cfg.state_path)
    streams = state.setdefault("streams", {})
    stream = streams.get(stream_key)

    def notify(posts: List[Dict[str, Any]], *, is_test: bool) -> None:
        slack_text = build_slack_summary_text(posts=posts, feed_name=cfg.alert_feed_name, emoji=cfg.alert_emoji, is_test=is_test)
        slack_attachments = build_slack_attachments(posts=posts, feed_name=cfg.alert_feed_name, emoji=cfg.alert_emoji, is_test=is_test, base_url=cfg.base_url)

        if cfg.dry_run:
            print(slack_text)
            if cfg.notion_token and cfg.notion_page_id:
                print(f"[DRY-RUN] Notion 전송: {len(posts)}개 글")
            return

        if cfg.slack_webhook_url:
            send_slack_message(
                get_session(),
                webhook_url=str(cfg.slack_webhook_url),
                text=slack_text,
                attachments=slack_attachments,
                channel=cfg.slack_channel,
                username=cfg.slack_username,
            )

        if cfg.notion_token and cfg.notion_page_id:
            try:
                send_to_notion(
                    token=str(cfg.notion_token),
                    page_id=str(cfg.notion_page_id),
                    posts=posts,
                    feed_name=cfg.alert_feed_name,
                    emoji=cfg.alert_emoji,
                    is_test=is_test,
                    dry_run=cfg.dry_run,
                )
            except Exception as e:
                print(f"[WARN] Notion 전송 실패 (Slack은 정상 전송됨): {e}", file=sys.stderr)
        elif cfg.notion_token or cfg.notion_page_id:
            print("[WARN] Notion 전송 스킵: NOTION_TOKEN과 NOTION_PAGE_ID 둘 다 설정되어야 합니다.", file=sys.stderr)

    # 알림 대상이 하나도 없으면(실전 실행에서 Slack/Notion 모두 미설정) 조용히 종료하지 않고
    # 명시적으로 실패시켜 '알람이 안 오는' 무음 장애를 눈에 보이게 만듭니다. dry-run은 예외.
    has_notion_target = bool(cfg.notion_token and cfg.notion_page_id)
    if not cfg.dry_run and not cfg.init_only:
        _require(
            bool(cfg.slack_webhook_url) or has_notion_target,
            "SLACK_WEBHOOK_URL(또는 Notion 설정)이 없어 알림을 보낼 수 없습니다.",
        )

    # 현재 공지 목록 조회(정규화).
    # test-latest / init / 최초 실행 경로는 '현재 화면(1페이지)'만 필요하므로 page=1만 조회하며,
    # 이들 경로의 동작은 예전과 동일합니다. 실제 알림 경로에서만 아래에서
    # fetch_all_new_notices로 새 글이 없어질 때까지 여러 페이지를 이어서 조회합니다.
    raw_items = fetch_notices(get_session(), base_url=cfg.base_url, page=1)
    notices = [normalize_notice(it, base_url=cfg.base_url) for it in raw_items if int(it.get("id", 0)) > 0]
    current_ids = [p["id"] for p in notices]

    # ── 테스트 모드: 상태 저장 없이 최신 글 1건을 [TEST]로 전송 ──
    if cfg.test_latest:
        if not notices:
            text = "[TEST] 최신 글 조회 결과: 게시물이 없습니다."
            if cfg.dry_run:
                print(text)
                return 0
            _require(bool(cfg.slack_webhook_url), "SLACK_WEBHOOK_URL이 필요합니다. (dry-run이면 필요 없음)")
            send_slack_message(get_session(), webhook_url=str(cfg.slack_webhook_url), text=text, channel=cfg.slack_channel, username=cfg.slack_username)
            return 0
        # 목록 첫 항목은 '중요(고정)' 공지일 수 있어, id가 가장 큰(가장 최근 생성) 글을 최신으로 사용
        latest = max(notices, key=lambda p: p["id"])
        notify([latest], is_test=True)
        return 0

    # ── init 모드 / 최초 실행(상태 없음, send_on_first_run=False): 기준점만 저장 ──
    if cfg.init_only or (stream is None and not cfg.send_on_first_run):
        mode = "초기화" if cfg.init_only else "최초 실행"
        if cfg.dry_run:
            print(f"[DRY-RUN] {mode}: 현재 공지 {len(current_ids)}건을 기준점으로 저장할 예정(상태 저장 안 함).")
            return 0
        streams[stream_key] = {
            "seen_ids": _prune_seen_ids(current_ids),
            "updated_at": _utc_now_iso(),
        }
        save_state(cfg.state_path, state)
        print(f"[OK] {mode}: 현재 공지 {len(current_ids)}건을 기준점으로 저장했습니다. 이후부터 새 글만 알림됩니다.")
        print(f"      state: {cfg.state_path}")
        return 0

    # ── 새 글 판별 ──
    if stream is None:
        # send_on_first_run=True: 가장 최근 글 몇 개를 보내고 전체를 seen으로 저장
        seen_ids: set[int] = set()
    else:
        seen_ids = set(int(x) for x in stream.get("seen_ids", []))

    # 다운타임/버스트로 2페이지 이상 새 글이 쌓였을 수 있으므로, 실제 알림 경로에서는
    # seen_ids 기준으로 새 글이 없어질 때까지 페이지를 이어서 조회합니다.
    # (page=1만 조회하던 기존 동작은 단일 페이지 시나리오에서 결과가 동일합니다.)
    #
    # NOTE(의도된 tradeoff, FEAT-001): 위에서 init/test-latest/최초 실행 공용 뷰를 만들기
    # 위해 page=1을 이미 한 번 조회했고, 여기서 walker가 다시 page=1부터 시작하므로 실제
    # 알림 경로는 매 실행 page=1을 두 번 조회합니다. 위의 선(先)조회 결과를 walker의 첫
    # 페이지로 넘겨 중복을 없앨 수도 있지만, 그러면 walker 시그니처와 init/최초 실행/
    # test-latest 경로의 흐름이 복잡해집니다. 소스가 rate-sensitive해지지 않는 한 이 중복은
    # 그대로 두는 것이 코드가 더 읽기 쉽다는 판단으로 FEAT-001에서 명시적으로 수용했습니다.
    notices, current_ids = fetch_all_new_notices(
        get_session(), base_url=cfg.base_url, seen_ids=seen_ids
    )

    new_posts = [p for p in notices if p["id"] not in seen_ids]
    # 오래된 것부터(날짜, id) 정렬 — 날짜 동일/누락 시 id로 안정 정렬
    new_posts.sort(key=lambda p: (p["post_date"], p["id"]))

    if not new_posts:
        # 변화 없음: seen 목록만 최신 상태로 유지(고정공지 회전 등에 견고)
        if stream is not None and not cfg.dry_run:
            stream["seen_ids"] = _prune_seen_ids(list(seen_ids | set(current_ids)))
            stream["updated_at"] = _utc_now_iso()
            streams[stream_key] = stream
            save_state(cfg.state_path, state)
        return 0

    # 한 번에 너무 많으면 가장 최근 max_notify_per_run개만 전송(전부 seen 처리하여 재알림 방지)
    cap = max(1, cfg.max_notify_per_run)
    to_send = new_posts[-cap:] if len(new_posts) > cap else new_posts
    if len(new_posts) > cap:
        print(f"[INFO] 새 글 {len(new_posts)}건 중 최근 {cap}건만 전송합니다(나머지는 알림 생략).", file=sys.stderr)

    notify(to_send, is_test=False)

    if cfg.dry_run:
        # 미리보기: 상태를 변경하지 않습니다.
        print(f"[DRY-RUN] 새 글 {len(to_send)}건(상태 저장 안 함).")
        return 0

    streams[stream_key] = {
        "seen_ids": _prune_seen_ids(list(seen_ids | set(current_ids))),
        "updated_at": _utc_now_iso(),
    }
    save_state(cfg.state_path, state)
    print(f"[OK] 전송 완료: 새 글 {len(to_send)}건 알림, seen {len(streams[stream_key]['seen_ids'])}건 저장.")
    return 0


def _report_failure(cfg: Optional[Config], prefix: str, error: BaseException) -> None:
    """실행 실패를 stderr에 남기고, 가능하면 Slack 실패 알림도 시도합니다(best-effort)."""
    print(f"{prefix}: {error}", file=sys.stderr)
    # cfg가 아직 없거나(설정 단계 실패), dry-run이거나, webhook이 없으면 Slack 알림은 생략.
    if cfg is None or cfg.dry_run or not cfg.slack_webhook_url:
        return
    try:
        session = _requests_session()
        send_failure_notification(session, cfg=cfg, error_summary=str(error))
    except Exception as e:  # noqa: BLE001 - 실패 알림 실패가 원래 오류를 가리지 않도록.
        print(f"[WARN] 실패 알림 처리 중 추가 오류: {e}", file=sys.stderr)


def main() -> None:
    # cfg는 설정 단계에서 실패할 수 있으므로 먼저 None으로 두고, 만들어지면 실패 알림에 사용합니다.
    cfg: Optional[Config] = None
    try:
        args = parse_args(sys.argv[1:])
        cfg = build_config_from_env_and_args(args)
    except SystemExit:
        # argparse가 던지는 정상적인 종료(예: --help, 잘못된 인자)는 그대로 전파.
        raise
    except Exception as e:
        # 설정 구성 단계 실패: cfg가 없으므로 Slack 알림은 불가, 안전하게 비정상 종료.
        print(f"[ERROR] 설정 오류: {e}", file=sys.stderr)
        raise SystemExit(2)

    try:
        raise SystemExit(run(cfg))
    except SystemExit as e:
        # run()이 반환한 종료 코드. 0이면 정상 종료, 그 외에는 실패 알림 시도.
        code = e.code
        if code is None or code == 0:
            raise
        _report_failure(cfg, "[ERROR] 실행 실패", RuntimeError(str(code)))
        raise SystemExit(2)
    except requests.HTTPError as e:
        _report_failure(cfg, "[ERROR] HTTP 오류", e)
        raise SystemExit(2)
    except requests.RequestException as e:
        _report_failure(cfg, "[ERROR] 네트워크 오류", e)
        raise SystemExit(2)
    except Exception as e:
        _report_failure(cfg, "[ERROR] 예외", e)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
